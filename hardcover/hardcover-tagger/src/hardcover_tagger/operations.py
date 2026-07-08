"""Domain operations against the Hardcover GraphQL API."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from hardcover_tagger.client import GraphQLClient, GraphQLError

# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------

RESOLVE_TAGGING_SETUP = """
query ResolveTaggingSetup($slug: String!, $names: [String!], $limit: Int!) {
  me {
    id
    lists(
      where: {name: {_in: $names}}
      order_by: {name: asc}
      limit: $limit
    ) {
      id
      name
    }
  }
  books(where: {slug: {_eq: $slug}}, limit: 1) {
    id
    title
    slug
  }
}
"""

FETCH_LISTS_BY_NAME = """
query FetchListsByName($names: [String!], $limit: Int!) {
  me {
    id
    lists(
      where: {name: {_in: $names}}
      order_by: {name: asc}
      limit: $limit
    ) {
      id
      name
    }
  }
}
"""

FETCH_EXISTING_LIST_BOOKS = """
query FetchExistingListBooks($book_id: Int!, $list_ids: [Int!], $limit: Int!) {
  list_books(
    where: {book_id: {_eq: $book_id}, list_id: {_in: $list_ids}}
    limit: $limit
  ) {
    list_id
  }
}
"""

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

MUTATION_BATCH_SIZE = 25


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
class TaggingSetup:
    book: Book | None
    user_lists: list[UserList]


@dataclass
class ListWorkItem:
    name: str
    list_id: int
    created: bool


@dataclass
class ListResult:
    name: str
    success: bool
    created: bool
    error: str = ""


@dataclass
class BatchResponse:
    data: dict[str, Any]
    alias_errors: dict[str, str]
    global_error: str = ""


# ---------------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------------


def resolve_tagging_setup(
    client: GraphQLClient,
    slug: str,
    requested_names: list[str],
) -> TaggingSetup:
    """Resolve the target book and any requested lists that already exist."""
    names = _unique_ordered(requested_names)
    data = client.execute(
        RESOLVE_TAGGING_SETUP,
        {"slug": slug, "names": names, "limit": _list_lookup_limit(names)},
        query_name="ResolveTaggingSetup",
    )
    return TaggingSetup(
        book=_parse_book(data),
        user_lists=_parse_user_lists(data, "ResolveTaggingSetup"),
    )


def resolve_lists_by_name(client: GraphQLClient, names: list[str]) -> list[UserList]:
    """Resolve only the named lists for retry work."""
    names = _unique_ordered(names)
    data = client.execute(
        FETCH_LISTS_BY_NAME,
        {"names": names, "limit": _list_lookup_limit(names)},
        query_name="FetchListsByName",
    )
    return _parse_user_lists(data, "FetchListsByName")


def resolve_existing_list_book_ids(
    client: GraphQLClient,
    book_id: int,
    list_ids: list[int],
) -> set[int]:
    """Return list IDs that already contain the target book."""
    list_ids = _unique_ordered(list_ids)
    if not list_ids:
        return set()

    data = client.execute(
        FETCH_EXISTING_LIST_BOOKS,
        {"book_id": book_id, "list_ids": list_ids, "limit": len(list_ids)},
        query_name="FetchExistingListBooks",
    )
    return {
        item["list_id"]
        for item in data.get("list_books", [])
        if isinstance(item.get("list_id"), int)
    }


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
    """Create missing lists as needed, then add the book to each requested list."""
    results: list[ListResult] = []
    by_name: dict[str, int] = {lst.name: lst.id for lst in user_lists}
    matched, unmatched = match_lists_by_name(user_lists, existing_names)

    for name in unmatched:
        results.append(
            ListResult(
                name=name,
                success=False,
                created=False,
                error=f"List not found: {name}",
            )
        )

    work: list[ListWorkItem] = []
    scheduled_names: set[str] = set()
    for name, list_id in matched.items():
        work.append(ListWorkItem(name=name, list_id=list_id, created=False))
        scheduled_names.add(name)

    missing_new_names: list[str] = []
    for name in _unique_ordered(new_names):
        list_id = by_name.get(name)
        if list_id is None:
            missing_new_names.append(name)
            continue
        if name in scheduled_names:
            continue
        work.append(ListWorkItem(name=name, list_id=list_id, created=False))
        scheduled_names.add(name)

    if dry_run:
        work.extend(ListWorkItem(name=name, list_id=0, created=True) for name in missing_new_names)
        return results + [
            ListResult(name=item.name, success=True, created=item.created) for item in work
        ]

    created_work, create_failures = create_lists(client, missing_new_names)
    work.extend(created_work)
    results.extend(create_failures)

    existing_list_book_ids = resolve_existing_list_book_ids(
        client,
        book.id,
        [item.list_id for item in work],
    )
    pending_work: list[ListWorkItem] = []
    for item in work:
        if item.list_id in existing_list_book_ids:
            results.append(ListResult(name=item.name, success=True, created=item.created))
            continue
        pending_work.append(item)

    results.extend(add_book_to_lists(client, book.id, pending_work))
    return results


def create_lists(
    client: GraphQLClient,
    names: list[str],
) -> tuple[list[ListWorkItem], list[ListResult]]:
    """Create lists in aliased GraphQL mutation batches."""
    created_work: list[ListWorkItem] = []
    failures: list[ListResult] = []

    for chunk in _chunks(_unique_ordered(names), MUTATION_BATCH_SIZE):
        mutation, variables, aliases = _build_create_lists_mutation(chunk)
        response = _execute_batch(client, mutation, variables, "CreateLists")

        for name, alias in aliases:
            error = _operation_error(response, alias)
            if error:
                failures.append(_failed_result(name, created=True, error=error))
                continue

            result = response.data.get(alias)
            list_id, error = _parse_created_list_id(result)
            if error:
                failures.append(_failed_result(name, created=True, error=error))
                continue
            created_work.append(ListWorkItem(name=name, list_id=list_id, created=True))

    return created_work, failures


def add_book_to_lists(
    client: GraphQLClient,
    book_id: int,
    work: list[ListWorkItem],
) -> list[ListResult]:
    """Add one book to many lists in aliased GraphQL mutation batches."""
    results: list[ListResult] = []

    for chunk in _chunks(work, MUTATION_BATCH_SIZE):
        mutation, variables, aliases = _build_add_books_mutation(book_id, chunk)
        response = _execute_batch(client, mutation, variables, "AddBookToLists")

        for item, alias in aliases:
            error = _operation_error(response, alias)
            if error:
                results.append(_failed_result(item.name, created=item.created, error=error))
                continue
            if not _has_inserted_id(response.data.get(alias)):
                msg = "No ID returned from insert_list_book"
                results.append(_failed_result(item.name, created=item.created, error=msg))
                continue
            results.append(ListResult(name=item.name, success=True, created=item.created))

    return results


def retry_failed(
    client: GraphQLClient,
    book: Book,
    failed_results: list[ListResult],
    user_lists: list[UserList],
) -> list[ListResult]:
    """Retry failed list additions once. Returns updated results."""
    retry_work = _retry_work(failed_results, user_lists)
    retried_results = add_book_to_lists(client, book.id, retry_work)
    by_name = {result.name: result for result in retried_results}
    return [by_name.get(result.name, result) for result in failed_results]


def _retry_work(failed_results: list[ListResult], user_lists: list[UserList]) -> list[ListWorkItem]:
    by_name = {lst.name: lst.id for lst in user_lists}
    retry_work: list[ListWorkItem] = []

    for result in failed_results:
        if "not found" in result.error.lower():
            continue
        list_id = by_name.get(result.name)
        if list_id is None:
            continue
        retry_work.append(ListWorkItem(name=result.name, list_id=list_id, created=result.created))

    return retry_work


def _parse_book(data: dict[str, Any]) -> Book | None:
    books = data.get("books", [])
    if not books:
        return None
    book = books[0]
    return Book(id=book["id"], title=book["title"], slug=book["slug"])


def _parse_user_lists(data: dict[str, Any], query_name: str) -> list[UserList]:
    me_list = data.get("me", [])
    if not me_list or not me_list[0].get("id"):
        msg = "Could not resolve user ID from API token"
        raise GraphQLError([{"message": msg}], query_name)

    return [UserList(id=item["id"], name=item["name"]) for item in me_list[0].get("lists", [])]


def _build_create_lists_mutation(
    names: list[str],
) -> tuple[str, dict[str, Any], list[tuple[str, str]]]:
    variables: dict[str, Any] = {}
    variable_defs: list[str] = []
    fields: list[str] = []
    aliases: list[tuple[str, str]] = []

    for index, name in enumerate(names):
        alias = f"create{index}"
        variable_name = f"name{index}"
        variable_defs.append(f"${variable_name}: String!")
        fields.append(f"  {alias}: insert_list(object: {{name: ${variable_name}}}) {{ id errors }}")
        variables[variable_name] = name
        aliases.append((name, alias))

    mutation = "mutation CreateLists(" + ", ".join(variable_defs) + ") {\n"
    mutation += "\n".join(fields)
    mutation += "\n}"
    return mutation, variables, aliases


def _build_add_books_mutation(
    book_id: int,
    work: list[ListWorkItem],
) -> tuple[str, dict[str, Any], list[tuple[ListWorkItem, str]]]:
    variables: dict[str, Any] = {"book_id": book_id}
    variable_defs = ["$book_id: Int!"]
    fields: list[str] = []
    aliases: list[tuple[ListWorkItem, str]] = []

    for index, item in enumerate(work):
        alias = f"add{index}"
        variable_name = f"list_id{index}"
        variable_defs.append(f"${variable_name}: Int!")
        fields.append(
            f"  {alias}: insert_list_book("
            f"object: {{list_id: ${variable_name}, book_id: $book_id}}"
            ") { id }"
        )
        variables[variable_name] = item.list_id
        aliases.append((item, alias))

    mutation = "mutation AddBookToLists(" + ", ".join(variable_defs) + ") {\n"
    mutation += "\n".join(fields)
    mutation += "\n}"
    return mutation, variables, aliases


def _execute_batch(
    client: GraphQLClient,
    mutation: str,
    variables: dict[str, Any],
    query_name: str,
) -> BatchResponse:
    try:
        return BatchResponse(
            data=client.execute(mutation, variables, query_name=query_name),
            alias_errors={},
        )
    except GraphQLError as exc:
        return BatchResponse(
            data=exc.data,
            alias_errors=_errors_by_alias(exc.errors),
            global_error=_global_error(exc.errors),
        )


def _errors_by_alias(errors: list[dict[str, Any]]) -> dict[str, str]:
    by_alias: dict[str, str] = {}
    for error in errors:
        path = error.get("path") or []
        if not path or not isinstance(path[0], str):
            continue
        alias = path[0]
        message = _error_message(error)
        if alias in by_alias:
            by_alias[alias] = f"{by_alias[alias]}; {message}"
        else:
            by_alias[alias] = message
    return by_alias


def _global_error(errors: list[dict[str, Any]]) -> str:
    messages: list[str] = []
    for error in errors:
        path = error.get("path") or []
        if path and isinstance(path[0], str):
            continue
        messages.append(_error_message(error))
    return "; ".join(messages)


def _operation_error(response: BatchResponse, alias: str) -> str:
    if alias in response.alias_errors:
        return response.alias_errors[alias]
    return response.global_error


def _parse_created_list_id(result: Any) -> tuple[int, str]:
    if not isinstance(result, dict):
        return 0, "No response returned from insert_list"
    errors = result.get("errors")
    if errors:
        return 0, str(errors)
    list_id = result.get("id")
    if not isinstance(list_id, int):
        return 0, "No ID returned from insert_list"
    return list_id, ""


def _has_inserted_id(result: Any) -> bool:
    return isinstance(result, dict) and isinstance(result.get("id"), int)


def _failed_result(name: str, *, created: bool, error: str) -> ListResult:
    return ListResult(name=name, success=False, created=created, error=error)


def _error_message(error: dict[str, Any]) -> str:
    return str(error.get("message", error))


def _unique_ordered(names: list[str]) -> list[str]:
    seen: set[str] = set()
    unique: list[str] = []
    for name in names:
        if name in seen:
            continue
        seen.add(name)
        unique.append(name)
    return unique


def _list_lookup_limit(names: list[str]) -> int:
    return max(1, len(names))


def _chunks[T](items: list[T], size: int) -> list[list[T]]:
    return [items[index : index + size] for index in range(0, len(items), size)]
