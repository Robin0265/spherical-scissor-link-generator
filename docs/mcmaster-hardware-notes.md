# McMaster hardware integration — note to self

_Last updated 2026-11-01._

## Where we landed

The tool needs **no McMaster login and no API credentials**. The only manual
step is getting each part's 3-D STEP once; everything after that is automatic:
detect the file, measure its dimensions, autofill `bearing_OD` /
`bearing_thickness`, block Generate/Update on conflicts (with the
reason shown in red), and import + place the parts at every joint.

## Getting a part's CAD — three routes

| Route | Login? | Automatic? | Does the add-in use it today? |
|---|---|---|---|
| **A. Fusion's built-in McMaster-Carr insert** | No | No (you click) | **Not yet** — see below |
| **B. Download the STEP in your browser** | No | No (you click) | **Yes** — the supported path |
| **C. McMaster Product Information API** | Certificate + account | Yes | Yes, if configured |

### A. Fusion's built-in *Insert McMaster-Carr Component*

Fusion ships this as a built-in command (**Insert ▸ Insert McMaster-Carr
Component**, command id `InsertMcMasterCarrComponentCommand`, in the Insert
panel). It opens mcmaster.com inside Fusion: pick the part, open **Product
Detail**, choose **3-D STEP** (or SAT — the only 3-D formats it accepts) and
**Save**, and the part is inserted straight into the active design. No
McMaster account or login is needed — CAD download on mcmaster.com is free and
anonymous for a person clicking.

What it does **not** change: it is interactive. The Fusion API can open the
dialog (`commandDefinitions.itemById('InsertMcMasterCarrComponentCommand')
.execute()`), but it has no access to the page embedded in it, so a script
cannot choose a part or press Save. It is the no-login route for a human, not
a way to automate the download.

**How it works inside** (from the plugin's own resources and
`NaFusionLibrariesUIPlugin10.dll`, 2026-11-01): it is an embedded Chromium
browser (Qt WebEngine) pointed at the public `https://www.mcmaster.com` - no
API, key or special endpoint. It watches the browser's downloads, redirects
the file to its own folder, rejects anything but STEP/SAT, and imports it as a
new component. "No login" just means anonymous human browsing.

**Tried and dropped (2026-11-01): our own McMaster panel inside Fusion.** An
add-in palette opened on `https://www.mcmaster.com/<PN>/` first hit a login
prompt (Chrome on the same connection did too), and after logging in McMaster
showed "Access has been restricted ... your use exceeds typical patterns of
our visitors". McMaster rate-limits page views per visitor (account, network
address, browser session) and actively polices unusual traffic, so any
in-Fusion browsing/automation route is a dead end and risks the account. The
add-in's palette API also exposes no download events. If a restriction
appears: stop visiting the site for a few hours, or contact McMaster.

**Not built yet:** the add-in only reads STEP files (cache → Downloads → API).
A part inserted with this command lands as a component in the design, which
the add-in currently ignores. The natural next step is a pickup that finds an
inserted McMaster component whose part number matches the dialog field,
measures it (the same `_part_frame` logic `hardware.place` already uses) and
places copies of it — no STEP file involved. Until then, use route B.

### B. Browser download into Downloads (current workflow)

1. Type the part numbers in the dialog's **McMaster-Carr hardware** group.
2. Press **Open part pages** — it opens each part not already available
   locally on mcmaster.com in your default browser.
3. Download the **3-D STEP** into Downloads. McMaster names it
   `<PN>_<description>.STEP`.
4. Back in the dialog, the file is picked up automatically on the next
   interaction (or press **Fetch** / **Generate**): it is copied to
   `.mcmaster-cache/`, measured, and the parameters autofill.

The Downloads folder is read from the Windows registry (so a OneDrive-
redirected Downloads works), with `~/Downloads` and `%OneDrive%/Downloads` as
fallbacks.

### C. The Product Information API (true zero-click)

Still the only sanctioned way to *script* the download. Per
https://www.mcmaster.com/help/api/ (checked 2026-11-01):

- For **approved customers** only. Each gets a **client certificate** + its
  password, attached to every request.
- `POST /v1/login` with your mcmaster.com username/password returns a bearer
  token (24 h). Product info responses carry `3-D STEP` links fetched via
  `GET /v1/cad/*` (rate-limited).
- To start: email **eCommerce@mcmaster.com** (the address the page lists now).

The code path exists (`mcmaster.Session`). Once you have the certificate:

1. If it's a `.pfx`, convert once:
   `openssl pkcs12 -in mcmaster.pfx -out mcmaster-client-cert.pem -nodes`
2. Create `mcmaster_credentials.json` in the repo root (gitignored):
   ```json
   {
     "certificate": "C:/path/to/mcmaster-client-cert.pem",
     "certificate_password": "",
     "username": "your mcmaster.com email",
     "password": "your mcmaster.com account password"
   }
   ```
   Two different passwords: `certificate_password` is empty if you converted
   with `-nodes`; `username`/`password` are the account login.
3. From then on Fetch pulls metadata over HTTP and Generate downloads +
   imports the STEP with no clicks.

### Why there is no credential-free automatic download

Fusion's insert and the website need no login because a *person* is
clicking. *Scripted* requests to mcmaster.com's own data/CAD endpoints are
refused (403): they require per-request tokens minted by the site's in-browser
JavaScript, which is bot protection this tool deliberately does not try to
get around. The API (route C) is the sanctioned alternative.

## Sizing rules the dialog enforces

The boss is a cylinder of diameter `bar_width`, so for fetched parts:

- thrust bearing OD ≤ `bar_width`;
- bushing OD < `bar_width` (its press-fit bore must leave a wall);
- bushing length ≤ `bar_thickness`: the bushing sits inside the bar,
  centred on it (changed 2026-11-01 - it used to set the boss height through
  `l_offset`; `l_offset` is now yours and never autofilled);
- `bearing_thickness` = bearing thickness and `bearing_OD` (boss bore) =
  bushing OD (both autofilled);
- bearing bore ≥ bushing bore, so one pin passes through both.

Any violation disables Generate/Update and the status line says which rule.
Example: thrust bearing 7806K58 (OD 12, ID 6, t 4.5 mm) with bushing 6658K727
(OD 10.02, ID 6, L 6 mm) needs `bar_width` ≥ 12 mm and `bar_thickness` ≥ 6 mm;
at the 8 mm / 3 mm defaults all three rules fail.

## Fixed 2026-11-01

- **Partial part numbers were cached.** Auto-resolve runs on every keystroke,
  and the Downloads match was a bare prefix test, so typing `7806K58` made
  `7`, `78`, … `7806K5` each "find" `7806K58_….STEP` and cache a copy under
  the partial number. Matching is now exact up to the `_`/space separator,
  and nothing that isn't a complete part number (`\d+[A-Z]{1,2}\d+`) is
  looked up. Old `7.step`, `780.dims.json`, … entries in `.mcmaster-cache/`
  are never read again and can be deleted.
- **Silent disabled Update.** When fetched parts conflicted with the geometry,
  Update was greyed out but the status line showed only the green autofill
  message. The status line now shows the blocking reason in red, from the
  same check that validation uses.
- **Bushing wider than the boss wasn't caught.** Now a hard stop (rule above).

## Verified earlier (2026-08-28, live in Fusion)

- End-to-end placement with real exported STEPs: n=2 → 7 thrust bearings at
  exactly radius R, 7+7 bushings at R ± (bearing_thickness/2 + boss_height/2),
  zero problems, timeline collapsed into one group.
- Fixed by live testing then: the STEP import's own timeline group collided
  with the run's group (now ungrouped); occurrence transforms are set at
  creation (transform overrides didn't survive); Fetch is metadata-only with
  a progress dialog; `ensure_step` acquires CAD at Generate.

## Tests

`tests/test_mcmaster.py` (stdlib only, no Fusion, never contacts
mcmaster.com). From the repo root:

    python -m unittest discover -s tests -v

The online tests run the real API code against a local stand-in for the
McMaster API: an HTTPS server that requires a client certificate and speaks
the documented formats (login token, product spec JSON, `/v1/cad` download,
`ErrorMessage` errors including `EXPIRED_AUTHORIZATION_TOKEN`). Certificates
are generated per run with the `openssl` CLI (on this machine:
`C:\Strawberry\c\bin\openssl.exe` - put it on PATH); without it those tests
skip. 20/20 pass on Linux (Python 3.10) and with Fusion's bundled Python
3.14.7 / OpenSSL 3.5.7 on Windows. Bugs they caught, fixed 2026-11-01:

- an expired API token (24 h, and the session lives as long as Fusion) made
  every lookup fail until restart - now it logs in again and retries once;
- the description in a downloaded file's name was dropped because the cache
  copy was measured instead of the original (cached dims had `description: ""`).

## Verified 2026-11-01 (live, in Fusion, cache-only)

Ran the add-in's own Update path (`_remember_part_numbers` →
`_resolve_for_build` → `_build`) on an existing mechanism with McMaster
lookups forced to the cache and all network calls blocked (none attempted):

- n=3, bar_width 12 mm, 7806K58 + 6658K727: autofill bearing_thickness 4.5,
  bearing_OD 10.02, l_offset 1.5 mm; 10 bearings at exactly R, 20 bushings at
  R ± 5.25 mm, every axis radial (0.000° deviation); no problems.
- Second Update through the feature's stored state, n=3 → 4: old run purged
  cleanly, 13 bearings / 26 bushings re-placed exactly, part numbers restored
  from the design attributes.
- **Fixed:** builds with hardware silently lost the editable custom feature
  (fell back to a plain group). The STEP import adds Base Features owned by
  the parts' own components, and a custom feature cannot span components. The
  feature now ends before the hardware stage (`wrap(end_index=...)`), and the
  occurrence + feature + hardware are folded into one collapsed timeline group
  named "Spherical Scissor". Double-click the feature inside the group to edit.
