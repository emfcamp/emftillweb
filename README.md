EMF Till web service
====================

Infrastructure needed to bring up an instance of `quicktill.tillweb`,
plus the public-facing web pages for https://bar.emf.camp/.

This is the EMF-specific fork of the project and contains assumptions
about how the EMF till is configured. [There is a separate repo for the generic version of the project here.](https://github.com/sde1000/tillweb)

The infrastructure code in `tillweb/config` has been modified as
little as possible. Most EMF-specific code is in `emf/`.


Prerequisites
-------------

You need [poetry](https://python-poetry.org/) and
[npm](https://www.npmjs.com/) installed. You need a
[postgresql](https://www.postgresql.org/) database available.


Configuration
-------------

The package reads `~/.config/emftillweb.toml` at startup. Example
suitable for development:

```toml
[django]
time_zone = "Europe/London"
mode = "devel"

[till]
database_name = "emfcamp"
currency_symbol = "£"
site_name = "EMF Bars"

[kiosk]
token_file = "/path/to/token"        # file containing the bearer token the kiosk sends in the Authorization header
till_user = 1                        # quicktill User id kiosk transactions are recorded under
barcode_secret = "<random-secret>"   # shared with quicktill-kiosk-plugin; HMAC barcode check digits
location = "SpaceBAR"                # the only stock location kiosk orders can be placed against
source = "kiosk"                     # optional; tag recorded on kiosk-created translines
expiry_source = "kiosk-expiry"       # optional; tag recorded on the expiry sweep's log entries
```

The `[kiosk]` section configures the kiosk order API:

- `token_file` — path to a file containing the shared bearer token the kiosk
  sends in the `Authorization: Bearer <token>` header. Use a long random
  string in production.
- `till_user` — the quicktill User id under whose name kiosk transactions are
  recorded.
- `barcode_secret` — shared with `quicktill-kiosk-plugin`, used to generate
  and verify the HMAC check digits on order barcodes.
- `location` — the single stock location kiosk orders can be placed against.
  Deployment is single-location: there is no per-request `location` param on
  any kiosk order endpoint (unlike the general stocklines endpoint below).


Development
-----------

To set up a development environment, ensure you have `poetry`
installed, and then in the project root run `poetry install` and `npm
install`.

Ensure there is a postgresql database called `emfcamp`. (`createdb
emfcamp` if unsure.) Either restore an existing quicktill database
dump into it (`zcat emfcamp.sql.gz | psql emfcamp` assuming the
`emfcamp` database is empty), or set up a new empty database using
`poetry run runtill -d dbname=emfcamp syncdb`

(If you are a bar team member, you can obtain a database dump from
your profile page.)

The various Django project management commands are then available via
`poetry run tillweb`, for example `poetry run tillweb check`, `poetry
run tillweb migrate`, `poetry run tillweb createsuperuser` and `poetry
run tillweb runserver` to run the development server.

The tillweb repository does not include the packed Javascript and CSS
necessary for the quicktill web interface project to run in
`tillweb/static/bundles/`. To regenerate these run `npm run build`.

The SCSS files in `emf/static/emf/scss/` can be converted to CSS by
running `npm run emfsass`, and you can start a process that watches
the SCSS files for changes by running `npm run emfsass-watch`.

After running `poetry install`, apply migrations:

```sh
poetry run tillweb migrate
```


Kiosk order API
---------------

The kiosk API lets a self-service kiosk place orders against the live till
database without needing direct database access. It is used by
[spacebar-kiosk](https://github.com/PolybiusBiotech/spacebar-kiosk), and
monitored by [spacebar-oms](https://github.com/PolybiusBiotech/spacebar-oms).
Orders recalled at the till are handled by the
[quicktill-spacebar-plugin](https://github.com/PolybiusBiotech/quicktill-spacebar-plugin).

| Endpoint | Auth | Purpose |
|---|---|---|
| `GET /api/stocklines.json?location=<name>` | None | Product list and live stock levels |
| `GET /api/kiosk/orders/` | Bearer token | List live kiosk orders (OMS poll) — every unpaid order plus every paid-but-not-yet-collected order |
| `POST /api/kiosk/orders/` | Bearer token | Place a new kiosk order. Body: `{ "items": [{ "stockline_id": 1, "qty": 1 }, ...] }`. Returns the order dict (see below) |
| `GET /api/kiosk/orders/<transid>/` | Bearer token | Retrieve a single order |
| `DELETE /api/kiosk/orders/<transid>/` | Bearer token | Cancel an unpaid order — no barcode check. 409 if already paid/closed or currently loaded at a till |
| `POST /api/kiosk/orders/<transid>/collect/` | Bearer token | Mark an order collected |
| `POST /api/kiosk/orders/<transid>/id-reject/` | Bearer token | Mark an order rejected (failed the kiosk's ID/age check) |

Order dict shape: `{ barcode, transaction_id, created_at, expires_at, soft_only, total, lines, paid, collected, cancelled, id_rejected }`.

`transaction_id` is the **quicktill Transaction ID** — no separate order-ref counter. `barcode` is a 10-digit code with no prefix: the first 5 digits are `transaction_id` run through a fixed permutation (so small transaction IDs don't print as a barcode with a run of leading zeros), and the last 5 are the last 5 decimal digits of HMAC-SHA1(`barcode_secret`, `transaction_id`). A valid barcode can only be issued by this server; this server and `quicktill-kiosk-plugin` must share the same `kiosk.barcode_secret`. (The badge still sends an `Order-Barcode` header on its cancel request; it isn't read — cancellation is authenticated by bearer token only.)

Unpaid orders expire 15 minutes after creation. A scheduled `expire_kiosk_orders` management command (run e.g. every minute via cron or a systemd timer) sweeps and deletes them; placing an order no longer triggers expiry as a side effect.


Seeding a fresh database
------------------------

If you are not restoring from a dump, you need to seed the `emfcamp`
database with test data before kiosk orders will work. **Run `syncdb`
first** — it populates `transcodes`, `stockremove`, and other reference
tables:

```sh
poetry run runtill -d dbname=emfcamp syncdb
```

Then seed test stock. Key constraints to be aware of:

- `Department` constructor kwarg is `id=`, **not** `dept=` (the Python attribute is `id`; the DB column is `dept`).
- Continuous `StockLine` rows must have **no** `dept_id`, `capacity`, or `pullthru` — those are only valid for `display`/`regular` linetypes.
- `StockItem` rows for a continuous line must have `stocklineid=None` and `checked=True`. If `stocklineid` is set, `StockType.stockonsale()` returns empty and the kiosk sees no stock.
- An active `Session` row (with `starttime` and `date`) is required before any order can be placed. Without it, the kiosk gets `no-active-session`.

After seeding, set a price for your test stocktype in the tillweb admin
(Stocktypes → set selling price for location `Spacebar`).
