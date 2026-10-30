# Python wheels for the web version

Pure-Python packages that Pyodide doesn't ship, stored here so the page never fetches code
from PyPI at run time. `worker.js` unzips them into Pyodide's site-packages the first time a
design uses them.

| File | Used for | Licence | SHA-256 |
|---|---|---|---|
| `ezdxf-1.4.5-py3-none-any.whl` | Reading DXF files (`vecfile._load_dxf`) | MIT (`ezdxf-LICENSE.txt`) | `70a79ec7d40990f60453e3c87ffe9f668907ee061cf6f4c0a3794d032ec2b40e` |

ezdxf's other needs (numpy, fontTools, pyparsing, typing-extensions) come with Pyodide.

To update: download the new `py3-none-any` wheel from PyPI
(`python -m pip download ezdxf==X --no-deps --only-binary=:all: --platform any -d web/wheels`),
replace the file, update its name in `worker.js` (`EZDXF_WHEEL`) and the table above, bump
`?v=` in `index.html`, and test a DXF in the browser.
