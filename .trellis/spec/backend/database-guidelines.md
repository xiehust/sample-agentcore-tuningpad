# Database Guidelines

> Database patterns and conventions for this project.

---

## Overview

<!--
Document your project's database conventions here.

Questions to answer:
- What ORM/query library do you use?
- How are migrations managed?
- What are the naming conventions for tables/columns?
- How do you handle transactions?
-->

(To be filled by the team)

---

## Query Patterns

<!-- How should queries be written? Batch operations? -->

(To be filled by the team)

---

## Migrations

<!-- How to create and run migrations -->

(To be filled by the team)

---

## Naming Conventions

<!-- Table names, column names, index names -->

(To be filled by the team)

---

## Common Mistakes

<!-- Database-related mistakes your team has made -->

(To be filled by the team)

---

## Schema changes (actual convention)

The database is SQLite and has no migration tool. `init_db()` runs `create_all` and then
`_add_missing_columns`, which adds every **nullable** model column that an existing table
lacks. To add a column:

- declare it nullable (`Mapped[str | None]`), so an old `data/tuningpad.db` keeps working
  after the upgrade;
- never add a NOT NULL column to an existing table; it is not added in place and queries on
  an old database fail;
- cover it the way `test_init_db_adds_new_nullable_columns` does.
