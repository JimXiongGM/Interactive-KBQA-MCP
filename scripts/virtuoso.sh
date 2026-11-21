#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."

# Extract the Virtuoso binary from the official Linux x86-64 release.
version=7.2.16.1
install_dir="$PWD/.cache/virtuoso-$version"
archive=virtuoso-opensource.x86_64-generic_glibc25-linux-gnu.tar.gz
if [[ ${1:-} == install ]]; then
    if [[ ! -x "$install_dir/bin/virtuoso-t" ]]; then
        mkdir -p .cache
        temporary=$(mktemp -d "$PWD/.cache/virtuoso.XXXXXX")
        trap 'rm -rf -- "$temporary"' EXIT
        curl -fL --retry 3 --connect-timeout 15 \
            "https://github.com/openlink/virtuoso-opensource/releases/download/v$version/$archive" \
            -o "$temporary/$archive"
        printf '%s  %s\n' 940e3ab593328f60f81fcea3cacfe0c054c4617cc5a64ef49c754bc4e35bc90c "$temporary/$archive" \
            | sha256sum --check
        tar -xzf "$temporary/$archive" --strip-components=1 -C "$temporary" \
            virtuoso-opensource/bin/virtuoso-t
        test -x "$temporary/bin/virtuoso-t"
        rm -- "$temporary/$archive"
        mv -T -- "$temporary" "$install_dir"
        trap - EXIT
    fi
    printf 'Virtuoso %s installed in %s\n' "$version" "$install_dir"
    exit 0
fi

# Run one process per database and open the local snapshot in place.
case ${1:-} in
    freebase)
        http_port=${FREEBASE_PORT:-29501}
        sql_port=${FREEBASE_SQL_PORT:-39501}
        ;;
    wikidata_kqapro)
        http_port=${KQAPRO_PORT:-29500}
        sql_port=${KQAPRO_SQL_PORT:-39500}
        ;;
    *)
        echo "Usage: $0 {install|freebase|wikidata_kqapro}" >&2
        exit 2
        ;;
esac
database="$PWD/data/$1/virtuoso"
mkdir -p log
test -s "$database/virtuoso.db"
cd -- "$database"
exec 9>.run.lock
flock -n 9 || { echo 'Virtuoso is already running for this database.' >&2; exit 1; }

# Start the database with a minimal configuration and write logs to the project log directory.
cat > virtuoso.ini <<INI
[Database]
DatabaseFile = virtuoso.db
TransactionFile = virtuoso.trx
ErrorLogFile = ../../../log/$1.virtuoso.log
LockFile = virtuoso.lck
xa_persistent_file = virtuoso.pxa
TempStorage = TempDatabase

[TempDatabase]
DatabaseFile = virtuoso-temp.db
TransactionFile = virtuoso-temp.trx

[Parameters]
ServerPort = 127.0.0.1:$sql_port
NumberOfBuffers = 170000
MaxDirtyBuffers = 130000
ThreadsPerQuery = 1
ServerThreads = 16
CheckpointInterval = 60
DirsAllowed = .

[HTTPServer]
ServerPort = 127.0.0.1:$http_port
ServerThreads = 16
Charset = UTF-8

[SPARQL]
ResultSetMaxRows = 10000
MaxQueryExecutionTime = 60
INI

exec "$install_dir/bin/virtuoso-t" +foreground +configfile virtuoso.ini
