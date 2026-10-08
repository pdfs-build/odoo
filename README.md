# pdfs.build for Odoo

Print any Odoo document with a template designed on [pdfs.build](https://pdfs.build).

This repository is the published form of the `pdfs_build` module: one branch
per Odoo version, the module folder at the root, as the Odoo Apps store expects.

| Branch | Odoo |
| --- | --- |
| `19.0` | 19.0 |
| `18.0` | 18.0 |
| `17.0` | 17.0 |

The module's own documentation is in [`pdfs_build/README.md`](pdfs_build/README.md).

## Tests

Each branch runs the module's tests on the official Odoo image of its version:

```bash
docker run --rm --network host -v "$PWD:/mnt/addons:ro" \
  -e HOST=127.0.0.1 -e USER=odoo -e PASSWORD=odoo odoo:17 \
  odoo -d test -i pdfs_build --addons-path=/mnt/addons \
  --test-enable --test-tags /pdfs_build --stop-after-init --no-http
```

with a PostgreSQL reachable on `127.0.0.1:5432` (user and password `odoo`).

## License

LGPL-3, see [LICENSE](LICENSE).
