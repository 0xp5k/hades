# The targets

Each target is one parser from Python's standard library. `targets.py` wraps it so that bytes go in and one outcome
comes out. Read the wrapper for a target (`t_<name>` in `targets.py`) to see exactly what is called.

    python3 targets.py                   # list the targets
    python3 targets.py <target> <hex>    # try one input

A defect is an error the parser should not raise for bad input (anything other than its own error class or the
ValueError family), or a run longer than 5 seconds. Its label is `<target>: <ErrorType> in <function>`.

| Target | What it parses |
|---|---|
| `addresses` | email address lists (email.utils.getaddresses, parseaddr) |
| `ascii85` | Ascii85 and Base85 decoding |
| `configparser` | INI files |
| `cookiejar` | Set-Cookie and date helpers (http.cookiejar) |
| `cookies` | Cookie headers (http.cookies) |
| `email` | a whole email message with the modern policy: headers, MIME structure, re-serialising |
| `fraction` | fractions.Fraction from text |
| `gettext` | compiled translation catalogs (.mo files) |
| `htmlparser` | HTML (html.parser) |
| `httpheaders` | HTTP header blocks (http.client.parse_headers) |
| `idna` | the idna codec, both directions |
| `maildate` | email dates (parsedate_to_datetime) |
| `pickletools` | pickle opcode streams, decoded only (nothing is unpickled) |
| `plistlib` | Apple property lists, binary and XML |
| `punycode` | the punycode codec |
| `regex` | regular expression patterns: compiled only, never run |
| `shlex` | shell-style splitting |
| `strptime` | date parsing: the input is '<value>' 0x00 '<format>' |
| `tarfile` | tar archives: headers are read and members listed, nothing is extracted to disk |
| `tokenize` | Python source tokens |
| `tomllib` | TOML documents |
| `urlparse` | URLs and query strings |
| `wave` | WAV audio headers and frames |
| `zipfile` | zip archives, read in memory |
| `zoneinfo` | compiled time zone files (TZif), pure-Python reader |

Inputs are at most 4096 bytes. For text parsers the bytes are decoded as UTF-8.
