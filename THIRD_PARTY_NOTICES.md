# Third-party components

IBKR Dashboard is distributed under GNU GPL version 3. The complete license
is in `LICENSE`, alongside the application source.

Each desktop bundle contains a `licenses` directory with `inventory.json`, the
exact installed versions of its Python runtime dependencies, their available
license and copyright files, and the Python runtime license. The GPLv3 option
is used for Qt for Python and Qt Charts. The desktop bundles also contain these
components; their own licenses and copyright notices remain applicable:

| Component | License and source |
| --- | --- |
| Inter 4.1 font | [SIL Open Font License 1.1](app/assets/fonts/LICENSE.txt); [official source](https://github.com/rsms/inter/releases/tag/v4.1) |
| Qt for Python / PySide6 6.8.1, including Qt Charts and Shiboken | [Qt for Python licensing](https://doc.qt.io/qtforpython-6/); [Qt Charts GPLv3](https://doc.qt.io/qt-6.8/qtcharts-index.html#licenses); [Qt 6.8.1 source](https://download.qt.io/archive/qt/6.8/6.8.1/single/) and [PySide 6.8.1 source tag](https://github.com/pyside/pyside-setup/tree/v6.8.1) |
| Interactive Brokers TWS API 10.50.01 | [IBKR GPL announcement](https://www.interactivebrokers.com/docs/tws-api/changelog/2026/8/3); Python package installed from IBKR's official source archive |
| SQLCipher / sqlcipher3 | [SQLCipher licensing](https://www.zetetic.net/sqlcipher/open-source/); sqlcipher3 license ships with its source distribution |
| openpyxl | [MIT license](https://foss.heptapod.net/openpyxl/openpyxl/-/blob/branch/3.1/LICENCE.rst) |
| et_xmlfile (openpyxl dependency) | MIT and Python licenses, copied from the installed package |
| certifi | [MPL 2.0](https://github.com/certifi/python-certifi/blob/master/LICENSE) |
| protobuf (IBKR API dependency) | [BSD 3-Clause](https://github.com/protocolbuffers/protobuf/blob/main/LICENSE) |
| tzdata (Windows) | [Apache 2.0](https://github.com/python/tzdata/blob/master/LICENSE) |
| keyring (macOS) | [MIT license](https://github.com/jaraco/keyring/blob/main/LICENSE) |
| Python runtime and its OpenSSL libraries | The bundled Python license text is in `licenses/Python-LICENSE.txt`; OpenSSL 3 uses the included `licenses/Apache-2.0.txt`, while [pre-3.0 OpenSSL uses its OpenSSL and SSLeay licenses](https://openssl-library.org/source/license/index.html) |

The application source archive is generated from the same commit as the
installers. [GNU's GPL FAQ](https://www.gnu.org/licenses/gpl-faq.en.html#SourceAndBinaryOnDifferentSites)
explains source access when binaries and source are hosted on different sites.
Review the exact dependency inventory, the Qt third-party code shipped with
each bundle, and the upstream source availability for each release before
publishing. Qt documents its [third-party code and license statements](https://doc.qt.io/qt-6.8/licenses-used-in-qt.html).
