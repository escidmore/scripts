"""Apply the recorded human-reviewed decisions to the original local comparison.

Run: python3 output/2026-09-11/hardcover-audit/apply-manual-review.py
No network calls. all.tsv is the original comparison; reviewed-*.tsv supersede it.
"""
import csv
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent
EXPORT = Path('/Users/host/Downloads/hardcover-export-3064.csv')

def read(path, delimiter='\t'):
    with path.open(newline='', encoding='utf-8-sig') as f:
        return list(csv.DictReader(f, delimiter=delimiter))

def write(name, rows, fields):
    with (ROOT / name).open('w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fields, delimiter='\t')
        writer.writeheader()
        writer.writerows(rows)

# Explicitly approved after inspecting every original provisional pairing and
# the same-author export records. Exceptions are in manual-pairings.tsv.
APPROVED = set('''431465 18163 1402075 1594487 1451509 1563860 1563857
1563858 1175710 1093424 455367 1195091 1160415 584716 1083952 1981771
457209 1125830 1250823 1603697 1612275 1086190 637746 933069 933068
1459839 1103492 1019997 573703 1612399 649200 468512 1612290 1612272
1612332 1612330 1509604 748620 325996 263418 477964 436227 467520 442920
427867 459046 427474 380013 489681 445869 1204816 445706 455447 637923
637921 637920 2371526 1205399 1781745 1376948 1187438 450562 1906014
1194231 69945 436344 1157860 441041 1612376 1760730 1760732 1802084
1061514 943245 717828 465152 824769 1686901 558734 1510024 1545311
1476223 603189 2136277 1939850 1822495 1309659 1906176 1452345 751050
1825671 529348 1218545 506871 443363 1157515 1746111 1843064 1806683
2128120 813030 1822823 1661253 2064911 2467686 1459830 2065269'''.split())

# Reviewed against same-author records, series numbering and all blank-author
# export entries. These are absent from THIS EXPORT, not necessarily the catalog.
ABSENT = set('''Black Girl, Call Home
Quill and Still
Shifting Shadows
Accidental Duelist
Accidental Raider
Accidental Dragoon
Goddess Reborn!
Goddess Descension
Goddess Rising
Night Angel Nemesis
Forge of Destiny
This Quest Is Broken!
This Class Is Bonkers!
This Guild Is Batty!
This Plot Is Bananas!: A Comedy LitRPG Adventure (This Trilogy Is Broken, Book 4)
Lucifer's Daughter
How to Defeat a Demon King in Ten Easy Steps
Soul Music
Oberon's Meaty Mysteries: The Squirrel on the Train
The Purloined Poodle
Word Puppets
Seasonal Fears
Sinners on Sunset: Imp World
Exodus
Queen of the Damned
The Morning Star
Unholy Pleasures
Freehaven Online: Dragonsbane
It's Not What It Looks Like
Even Tree Nymphs Get the Blues
You Do You
Iron and Magic
Twain’s Feast
We Are Legion (We Are Bob)
Clean Sweep
Clean Sweep (Dramatized Adaptation)
Training Daze
Last Breath
Bare Bones
Harsh Cry of the Heron
Insurrection
Kushiel's Scion
Storm Front
A Discovery of Witches
Time's Convert
Blade of Tyshalle
Caine Black Knife
Shadow of Night
The Book of Life
Jingo
The Truth
Roseland: Volume 2
Roseland: Volume 3
Reaper Man
Night Watch
Hogfather
A Whisper of Solace
Roseland, Volume 1
Forge of Destiny: Volume 2
Junkyard Roadhouse
Wild Geese
The Last One
Port of Shadows
The Silver Spike
Perdido Street Station
Dead Lies Dreaming
Quantum of Nightmares
Season of Skulls
Fantasy Swap Online, Book 1
The Assassin's Blade
City of Lust
Bared Before the Gods
Becoming Hers
Pleasure Bound
Dragon Lady: A Gender Swapped LitRPG Adventure
The Taste of Women
Bioshifter: Volume 3
Gender Swapped in Space
PERfunctory afFECTION
With a Flip of the Coin
Reclamation'''.splitlines())

original = read(ROOT / 'all.tsv')
export = read(EXPORT, ',')
manual = read(ROOT / 'manual-pairings.tsv')
decisions = {r['abs_title']: r for r in manual}
assert len(decisions) == len(manual), 'Duplicate manual decision'
assert set(decisions) <= {r['title'] for r in original}, 'Unknown ABS title'
assert ABSENT <= {r['title'] for r in original}
assert not ABSENT & set(decisions)
by_id = {}
for row_number, r in enumerate(export, 2):
    r['_row'] = str(row_number)
    by_id.setdefault(r['Hardcover Book ID'], []).append(r)
for d in manual:
    assert all(i in by_id for i in d['book_ids'].split(',')), d

reviewed = []
for source in original:
    b = dict(source)
    b.update(review_decision='', review_note='', export_record_numbers='')
    ids = []
    if b['title'] in decisions:
        d = decisions[b['title']]
        ids = d['book_ids'].split(',')
        b['review_decision'] = 'paired manually'
        b['review_note'] = d['reason']
    elif 'probable' in b['match']:
        assert b['book_id'] in APPROVED, b
        ids = [b['book_id']]
        b['review_decision'] = 'provisional pairing accepted'
        b['review_note'] = 'Inspected title, author where supplied, and series context; descriptive suffix or sparse export metadata.'
        if b['book_id'] == '468512':
            b['review_note'] = 'Carniepunk is a 38-page Kevin Hearne record in Iron Druid, consistent with the standalone Demon Barker story, not the full anthology.'
        if b['book_id'] == '573703':
            b['review_note'] = 'ABS subtitle True Love Bites and series volume 1 agree; Fluff and Fangs variant not established.'
    elif b['issues'] == 'already corrected after export':
        b['review_decision'] = 'already corrected after export'
        b['review_note'] = 'User marked Read and Audible edition 33299804 created after this export.'
    elif not b['match']:
        assert b['title'] in ABSENT, b
        b['review_decision'] = 'not found in export after manual review'
        b['review_note'] = 'No corresponding work found after reviewing same-author titles, series and blank-author export entries; fuzzy candidates rejected.'
        if b['title']=='Exodus':
            b['review_note'] = 'Export Exodus is by Leon Uris, not Debra Dunbar; no Dunbar Exodus record.'
        if b['title']=='Insurrection':
            b['review_note'] = 'Export Insurrection is by Thomas M. Reid, not David Weber / Steve White.'
        if b['title']=='Bioshifter: Volume 3':
            b['review_note'] = 'Bioshifter volumes 1 and 2 are present; volume 3 is not. Vigor Mortis 3 is a different series.'
        b['issues'] = 'not found in export'
    else:
        b['review_decision'] = 'original exact match retained'
    if ids:
        # A multi-book pairing covers the source only if every constituent is Read.
        groups = [by_id[i] for i in ids]
        selected = [max(g, key=lambda r:(r['Status']=='Read', r['Media']=='Audio', r['Date Finished']==b['abs_finished'])) for g in groups]
        b['hardcover_title'] = ' | '.join(r['Title'] for r in selected)
        b['book_id'] = ','.join(ids)
        b['edition_id'] = ','.join(r['Hardcover Edition ID'] for r in selected)
        b['status'] = 'Read' if all(r['Status']=='Read' for r in selected) else ' | '.join(r['Status'] for r in selected)
        b['media'] = ' | '.join(dict.fromkeys(r['Media'] for r in selected))
        b['hardcover_finished'] = ' | '.join(r['Date Finished'] for r in selected)
        b['export_record_numbers'] = ','.join(r['_row'] for r in selected)
        b['match'] = 'manual book-level pairing'
        b['candidates'] = ''
        issues = []
        if b['status'] != 'Read':
            issues.append('not marked read')
        if any(not any(r['Status']=='Read' and r['Media']=='Audio' for r in g) for g in groups):
            issues.append('no read audiobook in export')
        if len(ids)>1 or any(t in b['review_note'] for t in ['adaptation', 'dramatized', 'edition scope', 'variant not established']):
            issues.append('edition scope needs review')
        if any(not any(r['Date Finished']==b['abs_finished'] for r in g) for g in groups):
            issues.append('finish date missing/different')
        b['issues'] = '; '.join(issues)
    reviewed.append(b)

unique = list({(b['title'],b['author']): b for b in reviewed}.values())
fields = list(reviewed[0])
write('reviewed-all.tsv', reviewed, fields)
write('reviewed-pairings.tsv', [b for b in unique if b['match']=='manual book-level pairing'], fields)
write('reviewed-not-read.tsv', [b for b in unique if 'not marked read' in b['issues']], fields)
write('reviewed-unmatched.tsv', [b for b in unique if not b['match'] and b['review_decision']!='already corrected after export'], fields)
write('reviewed-editions.tsv', [b for b in unique if 'no read audiobook' in b['issues'] or 'edition scope' in b['issues']], fields)
assert len(reviewed) == len(original)
lookup = {b['title']:b for b in unique}
assert lookup['Wayward: Running: An Isekai LitRPG']['book_id']=='2160613'
assert lookup['Wayward: Fighting: An Isekai LitRPG']['book_id']=='2187650'
assert lookup['Azarinth Healer: Book One: A LitRPG Adventure']['book_id']=='638219'
assert lookup['Beneath the Dragoneye Moons 6']['book_id']=='637835'
assert lookup['Silver in the Wood & Drowned Country']['status']=='Read'
assert lookup['Two Tales of the Iron Druid Chronicles']['book_id']=='1192413,429339'
assert lookup['Exodus']['review_decision']=='not found in export after manual review'
assert lookup['Bioshifter: Volume 3']['review_decision']=='not found in export after manual review'
assert lookup['Testimony of Mute Things']['review_decision']=='already corrected after export'
print('Review decisions:',dict(Counter(b['review_decision'] for b in unique)))
print('Distinct titles/authors:',len(unique),'ABS items:',len(reviewed))
print('Original unmatched resolved:',sum(not b['match'] and b['title'] in decisions for b in {(b['title'],b['author']):b for b in original}.values()))
print('Not Read:',sum('not marked read' in b['issues'] for b in unique))
print('Edition review:',sum('no read audiobook' in b['issues'] or 'edition scope' in b['issues'] for b in unique))
