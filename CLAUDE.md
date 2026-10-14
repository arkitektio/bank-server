# bank

Notes for whoever changes this service, person or agent.

## Migrations and jobs

A container of this image only serves. An installer (konstruktor) prepares the database with
`arkitekt-service run migrate` once per build — before the first start, and in an update before
anything is recreated — and runs everything else as a job the image offers by name
(`konstruktor job run bank <job>`). Nothing is migrated, seeded or repaired at start.

**Read the rules before changing a model, a management command or `bank_server/contract.py`:**
<https://github.com/arkitektio/arkitekt-service/blob/main/docs/migrations-and-jobs.md>. The ones that are broken most easily:

- A model change and its migration are one commit.
- Within a major, a migration leaves a schema the previous release still runs on: a failed
  update, and a rollback, start the old build on the migrated database. What cannot do that
  is a `feat!:`.
- A `manage.py` command an operator should be able to run on a hub is declared in `jobs=` in
  `bank_server/contract.py`; one nobody runs is deleted. Any job is safe to run again.
- What `setup=` names runs for every build, after the migrations: it changes nothing the
  second time and needs nothing but the database and the config.
- Existing data is rewritten by a data migration when that needs only the database (it runs
  once, with the service stopped), and by a re-runnable job in `setup=` when it needs the
  service's code or its storage. There is no upgrade step, and nothing is keyed to a version.

What this service declares:

- Setup, in order: `ensureadmin`, `restore_descriptors`. The second stores the descriptors of
  every row that misses a declared key (rows from before a key existed); `--all` describes
  every row again after a describer changed what a key means.
- Other jobs: none.
- Not jobs, on purpose: `import_finanzguru` reads a file from the machine it runs on, which a job's container does not have: it is a developer's tool, and the same import is offered to clients as `createFinanzguruImport`.

`tests/test_prepared.py` holds the contract to this: migrations committed, every job a command
of this service, the setup run twice. In `deployments/next` the service runs
`arkitekt-service standalone --debug`, which migrates and then serves: restart its container
to apply a new migration.

## Descriptors

Every descriptor is stored (`stored_descriptors`, recomputed on every save by
`arkitekt_service`), so a port's `requires` is the `requires` argument of a list filter, one
indexed query. The describers are in `bank_server/descriptors.py` and read only the row (a
relation by its key), so a transaction can be described before it is inserted.

Transactions are written in bulk, and no bulk write sends `pre_save`. Every one keeps the column
right: `bulk_create` and `bulk_update` stamp their rows first (`service.stamp`, with
`stored_descriptors` among the updated fields), and an `update()` of a field a descriptor reads
is followed by `restore_transactions(ids)` / `restore_connection(id)` / `restore_categories(ids)`
(`bank_server/service.py`), with the ids taken before the update when its filter is what it
changes. A path that loads transactions with `only()` loads `TRANSACTION_FIELDS` with them.
Restoring transactions announces nothing (a sync is announced once, on its account); restoring a
connection or category announces a changed one as UPDATED.
