"""Restore a trusted Wasla custom archive into an empty stock PostgreSQL database.

PGHOST, PGDATABASE and the other libpq settings select the restore target.
Data rows, indexes, constraints, function bodies and quoted SQL stay intact.
"""

import argparse
import os
from pathlib import Path
import re
import subprocess
import tempfile


IDENT = r'(?:[a-z_][a-z0-9_$]*|"(?:[^"\r\n]|"")+")'
NEON_ATTRIBUTE = re.compile(
    rf'^ALTER TABLE ONLY {IDENT}\.{IDENT} ALTER COLUMN {IDENT} SET \(pg_attver=[0-9]+\);[ \t]*$',
    re.MULTILINE,
)
EMPTY_TARGET = """DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM pg_namespace WHERE nspname !~ '^pg_' AND nspname NOT IN ('public','information_schema'))
       OR EXISTS (SELECT 1 FROM pg_class WHERE relnamespace='public'::regnamespace)
       OR EXISTS (SELECT 1 FROM pg_proc WHERE pronamespace='public'::regnamespace)
       OR EXISTS (SELECT 1 FROM pg_type WHERE typnamespace='public'::regnamespace)
       OR EXISTS (SELECT 1 FROM pg_extension WHERE extname <> 'plpgsql') THEN
        RAISE EXCEPTION 'Wasla restore requires an empty target database';
    END IF;
END $$;
"""


def portable_schema(schema):
    matches = {match.start(): match.end() for match in NEON_ATTRIBUTE.finditer(schema)}
    if not matches:
        return schema, 0
    # The PostgreSQL lexer distinguishes executable SQL from comments, dollar quotes
    # and string literals, including multiline literals containing a lookalike line.
    from pglast import scan

    pieces = []
    pos = 0
    previous = None
    for token in scan(schema):
        if token.name == 'ALTER' and token.start in matches and previous == 'ASCII_59':
            pieces.append(schema[pos : token.start])
            pos = matches[token.start]
        if token.name not in {'SQL_COMMENT', 'C_COMMENT'}:
            previous = token.name
    count = len(pieces)
    pieces.append(schema[pos:])
    return ''.join(pieces), count


def run_checked(args, stage, *, env=None, stdout=None):
    result = subprocess.run(
        args, stdout=stdout or subprocess.DEVNULL, stderr=subprocess.PIPE, env=env
    )
    if result.returncode:
        # PostgreSQL error details may include row contents, GUCs or connection strings.
        raise RuntimeError('PostgreSQL restore failed during ' + stage)


def restore(archive, target_env):
    if not target_env.get('PGDATABASE'):
        raise ValueError('Set PGDATABASE explicitly to an empty restore database')
    with tempfile.TemporaryDirectory(prefix='wasla-offline-restore-') as directory:
        root = Path(directory)
        pre, remaining = root / 'schema.sql', root / 'rows-and-constraints.sql'
        base = ['pg_restore', '--no-owner', '--no-acl', '--file=-']
        with pre.open('xb') as out:
            run_checked(
                base + ['--section=pre-data', str(archive)], 'schema extraction', stdout=out
            )
        schema, removed = portable_schema(pre.read_text(encoding='utf-8'))
        pre.write_text(EMPTY_TARGET + schema, encoding='utf-8')
        with remaining.open('xb') as out:
            run_checked(
                base + ['--section=data', '--section=post-data', str(archive)],
                'archive extraction',
                stdout=out,
            )
        # Both sections must be fully extracted before psql may start the atomic restore.
        run_checked(
            [
                'psql',
                '-X',
                '-q',
                '--set=ON_ERROR_STOP=on',
                '--single-transaction',
                '-f',
                str(pre),
                '-f',
                str(remaining),
            ],
            'transaction on empty target',
            env=target_env,
        )
    return removed


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('archive', type=Path)
    count = restore(parser.parse_args().archive, os.environ.copy())
    print(f'Atomic restore completed. Provider-specific column settings omitted: {count}')
