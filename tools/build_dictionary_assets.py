"""Reproduce bundled dictionaries from a checksum-verified ECDICT CSV; no network access."""
import argparse
import csv
import hashlib
import json
from pathlib import Path
import zipfile

from build_cet4_supplement import COMMIT, SOURCE, SOURCE_SHA256

FIELDS = ('word', 'pos', 'phonetic', 'translation', 'exchange')


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def compact_field(value):
    return value.replace('\t', ' ').replace('\r', ' ').replace('\n', ' ')


def build(source: Path, output: Path):
    if sha256(source.read_bytes()) != SOURCE_SHA256:
        raise ValueError('Source checksum mismatch; use the complete CSV at the documented commit')
    license_path = Path(__file__).resolve().parents[1] / 'LICENSES/ECDICT-MIT.txt'
    license_digest = sha256(license_path.read_bytes())
    if license_digest != 'f8552dd246f61a4e064569eae6194a01c6b3d63b03bf27c6ca863593c549ed0f':
        raise ValueError('Bundled MIT license must match the pinned upstream LICENSE bytes')
    entries = {}
    cet4 = {}
    rows = 0
    with source.open(encoding='utf-8-sig', newline='') as stream:
        for row in csv.DictReader(stream):
            rows += 1
            word = row['word'].strip().lower()
            if word:
                entries[word] = '\t'.join(compact_field(word if field == 'word' else row[field]) for field in FIELDS) + '\n'
            if 'cet4' in row['tag'].split():
                if word:
                    cet4[word] = compact_field(row['phonetic']).strip()
    if rows != 770611 or len(cet4) < 3000:
        raise ValueError('Unexpected source row count or CET-4 coverage')
    content = ''.join(entries.values()).encode('utf-8')
    wordlist = ''.join(word + ('\t' + cet4[word] if cet4[word] else '') + '\n' for word in sorted(cet4)).encode('utf-8')
    output.mkdir(parents=True, exist_ok=True)
    archive = output / 'ecdict_compact.tsv.zip'
    # Fix ZIP metadata so rebuilding the same input produces the same artifact.
    entry = zipfile.ZipInfo('ecdict_compact.tsv', date_time=(1980, 1, 1, 0, 0, 0))
    entry.create_system = 3
    entry.external_attr = 0o100644 << 16
    entry.compress_type = zipfile.ZIP_DEFLATED
    with zipfile.ZipFile(archive, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        z.writestr(entry, content, compresslevel=9)
    (output / 'cet4_words.txt').write_bytes(wordlist)
    metadata = dict(project='skywind3000/ECDICT', commit=COMMIT, source=SOURCE,
        sourceSha256=SOURCE_SHA256, sourceRows=rows,
        license='MIT', licenseFile='LICENSES/ECDICT-MIT.txt', licenseSha256=license_digest,
        licenseSource=f'https://raw.githubusercontent.com/skywind3000/ECDICT/{COMMIT}/LICENSE',
        dictionary=dict(file=archive.name, member='ecdict_compact.tsv', fields=list(FIELDS),
            rows=len(entries), sha256=sha256(archive.read_bytes()), memberSha256=sha256(content),
            transform='Trim and lowercase headwords; keep last duplicate value in first-occurrence order; replace literal tabs, CR and LF with spaces; UTF-8, LF.'),
        cet4=dict(file='cet4_words.txt', tag='cet4', wordCount=len(cet4), sha256=sha256(wordlist),
            transform='Select cet4 tag; trim and lowercase words; deduplicate and sort; trim phonetics; word plus optional TAB phonetic, LF.'))
    (output / 'dictionary_sources.json').write_text(json.dumps(metadata, indent=2) + '\n', encoding='utf-8')
    print(f'Built {len(entries)} dictionary entries from {rows} source rows and {len(cet4)} CET-4 words')
    return metadata


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path, help='Downloaded ecdict.csv at the pinned commit')
    parser.add_argument('--output', type=Path, default=Path(__file__).resolve().parents[1] / 'kandu/assets')
    args = parser.parse_args()
    build(args.source, args.output)
