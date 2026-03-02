"""Domain operations against the Hardcover GraphQL API."""

from __future__ import annotations

from dataclasses import dataclass

from hardcover_tagger.client import GraphQLClient, GraphQLError

# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------

RESOLVE_BOOK = """
query ResolveBook($slug: String!) {
  books(where: {slug: {_eq: $slug}}, limit: 1) {
    id
    title
    slug
  }
}
"""

FETCH_LISTS = """
query FetchUserLists($user_id: Int!, $limit: Int!, $offset: Int!) {
  lists(
    where: {user_id: {_eq: $user_id}},
    order_by: {name: asc},
    limit: $limit,
    offset: $offset
  ) {
    id
    name
  }
}
"""

# ---------------------------------------------------------------------------
# Mutations
# ---------------------------------------------------------------------------

CREATE_LIST = """
mutation CreateList($name: String!) {
  insert_list(object: {name: $name}) {
    id
    errors
  }
}
"""

ADD_BOOK_TO_LIST = """
mutation AddBookToList($list_id: Int!, $book_id: Int!) {
  insert_list_book(object: {list_id: $list_id, book_id: $book_id}) {
    id
  }
}
"""

ME_QUERY = """
query Me {
  me {
    id
  }
}
"""


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class Book:
    id: int
    title: str
    slug: str


@dataclass
class UserList:
    id: int
    name: str


@dataclass
class ListResult:
    name: str
    success: bool
    created: bool
    error: str = ""


# ---------------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------------

PAGE_SIZE = 100


def resolve_user_id(client: GraphQLClient) -> int:
    """Get the authenticated user's ID from the API."""
    data = client.execute(ME_QUERY, query_name="Me")
    me_list = data.get("me", [])
    if not me_list or not me_list[0].get("id"):
        msg = "Could not resolve user ID from API token"
        raise GraphQLError([{"message": msg}], "Me")
    return me_list[0]["id"]


def resolve_book(client: GraphQLClient, slug: str) -> Book | None:
    """Resolve a book slug to a Book, or None if not found."""
    data = client.execute(RESOLVE_BOOK, {"slug": slug}, query_name="ResolveBook")
    books = data.get("books", [])
    if not books:
        return None
    b = books[0]
    return Book(id=b["id"], title=b["title"], slug=b["slug"])


def fetch_all_lists(client: GraphQLClient, user_id: int) -> list[UserList]:
    """Fetch all lists for a user, paginating automatically."""
    all_lists: list[UserList] = []
    offset = 0
    while True:
        data = client.execute(
            FETCH_LISTS,
            {"user_id": user_id, "limit": PAGE_SIZE, "offset": offset},
            query_name="FetchUserLists",
        )
        batch = data.get("lists", [])
        for item in batch:
            all_lists.append(UserList(id=item["id"], name=item["name"]))
        if len(batch) < PAGE_SIZE:
            break
        offset += PAGE_SIZE
    return all_lists


def create_list(client: GraphQLClient, name: str) -> int:
    """Create a new list, returning its ID."""
    data = client.execute(CREATE_LIST, {"name": name}, query_name="CreateList")
    result = data.get("insert_list", {})
    errors = result.get("errors")
    if errors:
        raise GraphQLError([{"message": str(errors)}], "CreateList")
    list_id = result.get("id")
    if not list_id:
        raise GraphQLError([{"message": "No ID returned from insert_list"}], "CreateList")
    return list_id


def add_book_to_list(client: GraphQLClient, list_id: int, book_id: int) -> None:
    """Add a book to a list. Sequential only — no concurrent calls."""
    client.execute(
        ADD_BOOK_TO_LIST,
        {"list_id": list_id, "book_id": book_id},
        query_name="AddBookToList",
    )


def match_lists_by_name(
    existing_lists: list[UserList], names: list[str]
) -> tuple[dict[str, int], list[str]]:
    """Match list names to IDs. Returns (found, not_found)."""
    by_name: dict[str, int] = {lst.name: lst.id for lst in existing_lists}
    found: dict[str, int] = {}
    not_found: list[str] = []
    for name in names:
        if name in by_name:
            found[name] = by_name[name]
        else:
            not_found.append(name)
    return found, not_found


def process_lists(
    client: GraphQLClient,
    book: Book,
    existing_names: list[str],
    new_names: list[str],
    user_lists: list[UserList],
    *,
    dry_run: bool = False,
) -> list[ListResult]:
    """Add book to all specified lists. Creates new lists as needed.

    Returns a result per list name with success/failure status.
    """
    results: list[ListResult] = []

    # Match existing list names to IDs
    matched, unmatched = match_lists_by_name(user_lists, existing_names)
    for name in unmatched:
        results.append(
            ListResult(name=name, success=False, created=False, error=f"List not found: {name}")
        )

    # Build work items: (name, list_id, created)
    work: list[tuple[str, int, bool]] = [(name, lid, False) for name, lid in matched.items()]

    # Create new lists
    for name in new_names:
        if dry_run:
            results.append(ListResult(name=name, success=True, created=True))
            continue
        try:
            lid = create_list(client, name)
            work.append((name, lid, True))
        except (GraphQLError, Exception) as exc:
            results.append(ListResult(name=name, success=False, created=True, error=str(exc)))

    # Add book to each list — sequentially
    for name, lid, created in work:
        if dry_run:
            results.append(ListResult(name=name, success=True, created=created))
            continue
        try:
            add_book_to_list(client, lid, book.id)
            results.append(ListResult(name=name, success=True, created=created))
        except (GraphQLError, Exception) as exc:
            results.append(ListResult(name=name, success=False, created=created, error=str(exc)))

    return results


def retry_failed(
    client: GraphQLClient,
    book: Book,
    failed_results: list[ListResult],
    user_lists: list[UserList],
) -> list[ListResult]:
    """Retry failed list additions once. Returns updated results."""
    retried: list[ListResult] = []
    by_name = {lst.name: lst.id for lst in user_lists}

    for result in failed_results:
        # Skip "list not found" errors — they won't resolve on retry
        if "not found" in result.error.lower():
            retried.append(result)
            continue

        lid = by_name.get(result.name)
        if not lid:
            retried.append(result)
            continue

        try:
            add_book_to_list(client, lid, book.id)
            retried.append(
                ListResult(
                    name=result.name,
                    success=True,
                    created=result.created,
                )
            )
        except (GraphQLError, Exception) as exc:
            retried.append(
                ListResult(
                    name=result.name,
                    success=False,
                    created=result.created,
                    error=str(exc),
                )
            )
    return retried
