# Conduit backend: design note

Conduit is the API behind a small blogging site. Users sign up, write articles, follow each other
and comment.

## Storage

Users, articles, comments, favorites and follows live in PostgreSQL. The service connects with
`postgresql://conduit:secret@db:5432/conduit` and owns its schema.

The tag list is read on every page, so it is cached in Redis (`redis:7`) for sixty seconds and
rebuilt on a miss. Nothing else is cached.

## Notifications

When someone comments on an article, the author is told by email. The backend does not send mail
itself: it calls the Mail service, an internal HTTP service, once per comment
(`POST /messages`) and does not wait for delivery. The Mail service answers 202 with a message
id; a later `GET /messages/{id}` says whether it was delivered.

## Not a dependency

Images are served from a CDN that the browser talks to directly, so the backend never calls it.
