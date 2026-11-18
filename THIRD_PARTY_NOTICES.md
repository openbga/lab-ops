# Third-party software

OpenBGA Lab Ops is AGPL-3.0-only (copyright and trademark notice: [NOTICE](NOTICE)). Third-party packages retain their own licenses and notices.
The root LICENSE is the unmodified text retrieved from <https://www.gnu.org/licenses/agpl-3.0.txt>.
`uv.lock` identifies the exact Python distributions; installed distributions retain their license
files in the image. Do not strip those files or the notices in bundled browser assets.

| Component | Upstream license / source |
| --- | --- |
| Django | BSD-3-Clause; https://github.com/django/django/blob/main/LICENSE |
| django-allauth | MIT; https://github.com/pennersr/django-allauth/blob/main/LICENSE |
| fido2 (via django-allauth[mfa]) | BSD-2-Clause OR Apache-2.0 OR MPL-2.0 (Yubico); retain COPYING, COPYING.APLv2, COPYING.MPLv2 |
| qrcode (via django-allauth[mfa]) | BSD-3-Clause (Lincoln Loop), including code under the MIT license (Kazuhiko Arase); "QR Code" is a registered trademark of DENSO WAVE INCORPORATED |
| cryptography | Apache-2.0 OR BSD-3-Clause; retain LICENSE, LICENSE.APACHE, LICENSE.BSD (wheels bundle OpenSSL, Apache-2.0) |
| Psycopg / psycopg-binary | LGPL-3.0-only; https://github.com/psycopg/psycopg/blob/master/LICENSE.txt |
| Gunicorn | MIT; https://github.com/benoitc/gunicorn/blob/master/LICENSE |
| WhiteNoise | MIT; https://github.com/evansd/whitenoise/blob/main/LICENSE |
| Pillow | MIT-CMU; wheels bundle third-party libraries whose notices are in the installed LICENSE file |
| dj-database-url | BSD-3-Clause; https://github.com/jazzband/dj-database-url/blob/master/LICENSE |
| Bootstrap 5.3.8 | MIT; https://github.com/twbs/bootstrap/blob/v5.3.8/LICENSE; retain bundled license header/file under static/vendor/bootstrap-5.3.8/ |
| Popper (bundled in the Bootstrap JavaScript) | MIT. [Popper license and copyright](static/vendor/bootstrap-5.3.8/popper.LICENSE) |
| Bootstrap Icons 1.13.2 (subset in static/img/icons.svg, symbols copied verbatim) | MIT; https://github.com/twbs/icons/blob/v1.13.2/LICENSE; retain static/img/icons.LICENSE |
| Overpass (variable, latin and latin-ext subsets, via @fontsource-variable/overpass 5.3.0) | SIL Open Font License 1.1; Copyright 2021 The Overpass Project Authors (https://github.com/RedHatOfficial/Overpass); retain static/vendor/overpass-5.3.0/OFL.txt |
| HTMX 2.0.11 | 0BSD; https://github.com/bigskysoftware/htmx/blob/v2.0.11/LICENSE; retain bundled LICENSE under static/vendor/htmx-2.0.11/ |
| Tom Select 2.7.0 (including Sifter, Unicode Variants and microplugin) | Apache-2.0; https://github.com/orchidjs/tom-select/blob/v2.7.0/LICENSE; retain bundled license and headers under static/vendor/tom-select/ |
| Vanilla Calendar Pro 3.4.0 | MIT; https://github.com/uvarov-frontend/vanilla-calendar-pro/blob/v3.4.0/LICENSE; retain bundled LICENSE and the header under static/vendor/vanilla-calendar-pro-3.4.0/ |
| DataTables 3.1.5 / ColReorder 3.1.2 (with Bootstrap 5 integrations) | MIT; https://datatables.net/license/mit; retain bundled License.txt under static/vendor/datatables/ |
| Playwright / pytest-playwright (optional test tools) | Apache-2.0; https://github.com/microsoft/playwright-python/blob/main/LICENSE |

The container image is built on `python:3.14.10-slim-bookworm` (Debian 12) plus the `uv` binary
from `ghcr.io/astral-sh/uv`. The base image keeps the licences and notices recorded under
`/usr/share/doc/<package>/copyright`; Debian publishes the corresponding source for its packages.
Python wheels for Linux bundle native libraries whose notices ship in each package's metadata.
A redistributed binary/container release must include applicable corresponding
source and relinking materials required by its copyleft dependencies. Check the license files in the image when redistributing it.
