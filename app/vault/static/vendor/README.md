# Vendored browser assets

The browse console serves these files from `/vault/assets/`; it has no CDN or
frontend build dependency.

| Package | Version | Browser bundle SHA-256 | License |
| --- | --- | --- | --- |
| [Marked](https://github.com/markedjs/marked) | 18.0.11 | `69451c8541c9c1e7a4bf3ffc6f73c4d89633de92bfbe3e484dfe182ef8091f88` | MIT; see `marked-18.0.11/LICENSE` |
| [DOMPurify](https://github.com/cure53/DOMPurify) | 3.4.15 | `f263b05369e050fa175d4ecb9c9358eb4253602d510297adfb31df48b2f1c4d5` | Apache-2.0 or MPL-2.0; distributed here under Apache-2.0, see `dompurify-3.4.15/LICENSE` |

The bundles and matching source maps came from their versioned npm packages
through jsDelivr. The license texts came from the corresponding Git tags.
When updating either dependency, replace its bundle, source map and license
together, then update the versioned path, template reference, checksums above,
and the pinned-asset cases in `tests/vault/test_browse_console.py`.
