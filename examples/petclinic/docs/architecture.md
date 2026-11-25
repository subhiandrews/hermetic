# PetClinic REST backend: design note

PetClinic is the API behind a veterinary clinic's front desk. Staff register pet owners and their
pets, record visits, and look up the vets and what each one specializes in.

## Storage

Owners, pets, pet types, visits, vets, specialties and users live in PostgreSQL. The service connects
with `postgresql://petclinic:secret@db:5432/petclinic` and owns its schema. Reference data (pet
types, specialties) is loaded when the schema is created.

Nothing is cached: every request reads the database.

## Other services

None. The backend does not call any other HTTP service, and nothing calls out to the internet.

## Not a dependency

The `/oops` endpoint always fails on purpose, so that clients and tests can see an error response.
It does not reach any other system.
