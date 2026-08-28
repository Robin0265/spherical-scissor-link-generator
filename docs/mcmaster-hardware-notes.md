# McMaster hardware integration — note to self

_Last updated 2026-08-28._

## Where we landed

We are on **Path 2: credential-free**. The one manual step is downloading each
part's 3-D STEP once from mcmaster.com; the tool does everything else
automatically (detect the file in Downloads, measure its dimensions, autofill
`bearing_OD` / `bearing_thickness` / `l_offset`, gate Generate on conflicts,
and import + place the parts at every joint).

The **Open part pages** button in the dialog opens the typed part numbers on
mcmaster.com in your own browser so you can grab the STEPs; it skips parts
already available locally. **Fetch** is metadata-only with a progress dialog
and never opens a browser. Generate/Update imports from the cache.

## Why not fully automatic (auto-download)?

Auto-download of the CAD is **not possible credential-free**, and this is a
McMaster policy, not a code limit:

- Their site data/CAD endpoints 403 any scripted request — the required
  per-request tokens are minted by their in-browser JavaScript (bot
  protection). Verified twice this session; the site eventually served this
  environment a blanket 403.
- The only sanctioned automation path is their **Product Information API**,
  which is gated behind a **client certificate** (not just a login).
- We confirmed via the live docs (https://www.mcmaster.com/help/api/) that the
  API has **no OAuth / SSO / browser-login popup** — certificate + username /
  password only. So a friendly "log in and we grab your credentials" flow
  cannot exist; the certificate is the gate and only McMaster can issue it.
- Fusion's built-in *Insert McMaster-Carr Component* isn't doing anything
  privileged: it's an embedded browser with a human clicking, on the free
  anonymous side of the fence. It needs no login because CAD download never
  required one — only *scripted* access is blocked.

## If you ever want true zero-click (Path 1, the API)

The code path already exists and its logic is verified; it just needs the
certificate. One-time setup:

1. Email **eprocurement@mcmaster.com** from your ordering account, ask to be
   enabled for the Product Information API (approval step — not instant).
2. They send a **client certificate** + a password for it.
3. If it's a `.pfx`, convert once:
   `openssl pkcs12 -in mcmaster.pfx -out mcmaster-client-cert.pem -nodes`
4. Create `mcmaster_credentials.json` in the repo root (gitignored):
   ```json
   {
     "certificate": "C:/path/to/mcmaster-client-cert.pem",
     "certificate_password": "",
     "username": "your mcmaster.com email",
     "password": "your mcmaster.com account password"
   }
   ```
   Note the **two different passwords**: `certificate_password` is empty if you
   converted with `-nodes`; `username`/`password` are your mcmaster.com account
   creds for the `/v1/login` call.
5. The moment that file exists, Fetch pulls metadata over HTTP and Generate
   auto-downloads + imports the STEP — no clicks.

Offered but not yet built: a **guided in-Fusion setup panel** (cert file
picker, Test-connection button, auto `.pfx`→PEM, writes the JSON). Ask for it
when you have the certificate.

## Verified this session (live, in Fusion)

- End-to-end placement with real exported STEPs: n=2 → 7 thrust bearings at
  exactly radius R, 7+7 bushings at R ± (bearing_thickness/2 + boss_height/2),
  zero problems, timeline collapsed into one group.
- Fixed three real bugs found only by live testing: the STEP import's own
  timeline group collided with the run's group (now ungrouped); occurrence
  transforms are set at creation (transform overrides didn't survive); Fetch
  is metadata-only with a progress dialog; `ensure_step` acquires CAD at
  Generate.
