# coast/db - persistence

`models.py` and `base_models.py` define the SQLAlchemy 2.x models.
`api.py` provides oslo.db enginefacade plumbing, project-scoped
repositories, uniqueness checks and task-state compare-and-swap helpers.

`migration/alembic_migrations/versions/` holds schema revisions. Never edit
a merged revision. Run `coast-db-manage upgrade` to apply migrations.
MySQL/MariaDB uses InnoDB and utf8mb4; unit tests use SQLite.
Spec: `doc/source/data-model.md`.
