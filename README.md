# pdfs.build for Odoo

Print any Odoo document with a template designed on [pdfs.build](https://pdfs.build).

This repository is the home of the `pdfs_build` module, published on the
[Odoo Apps store](https://apps.odoo.com/apps/modules/19.0/pdfs_build/): one
branch per Odoo version, the module folder at the root, as the store expects.
Changes land on `19.0` first and are cherry-picked to the older branches.

| Branch | Odoo |
| --- | --- |
| `19.0` | 19.0 |
| `18.0` | 18.0 |
| `17.0` | 17.0 |

The module's own documentation is in [`pdfs_build/README.md`](pdfs_build/README.md).

Templates whose fields are named after Odoo's own (the gallery's
[Odoo category](https://pdfs.build/templates/odoo-invoice/)) need no data
expression at all: choosing the model writes it. The contract those templates
follow lives in the pdfs.build repository under `showcase/odoo/README.md`.

## Tests

Each branch runs the module's tests on the official Odoo image of its version:

```bash
docker run --rm --network host -v "$PWD:/mnt/addons:ro" \
  -e HOST=127.0.0.1 -e USER=odoo -e PASSWORD=odoo odoo:19 \
  odoo -d test -i pdfs_build --addons-path=/mnt/addons \
  --test-enable --test-tags /pdfs_build --stop-after-init --no-http
```

with a PostgreSQL reachable on `127.0.0.1:5432` (user and password `odoo`).

## License

LGPL-3, see [LICENSE](LICENSE).
