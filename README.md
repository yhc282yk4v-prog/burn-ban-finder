# Burn Ban Finder

A map of open-burning bans and fire restrictions across the USA, centered on where you are.

    python3 server.py        # then open http://localhost:8765

No dependencies (Python 3 standard library only). Set `PORT=9000` to change the port.

## Keeping it current
- A background thread re-pulls every source every 5 minutes; the page re-fetches every 5 minutes and when the tab regains focus.
- Each source reports when its data was last edited. Seasonal-restriction layers (Forest Service, tribal land) are hidden
  automatically if they haven't been edited within 30 days; Texas uses the forest service's own daily list.
- Orders no map carries (New Mexico's statewide order, Arizona BLM status) are re-checked against the agency's page each refresh
  (`ORDERS` in `server.py`). If the page stops saying what was recorded, the order drops off the map instead of being asserted.
- **Hosted (free):** `.github/workflows/publish.yml` runs `python server.py --build site` every 5 minutes on GitHub Actions and
  publishes the result to GitHub Pages as static files (no server, no sleeping). GitHub's scheduler can run a few minutes late; the
  page shows how old its data is. Community reports are off on the hosted copy.
- Locally, nothing updates while `python3 server.py` isn't running.

## Data
- **State feeds (live, cached 15 min):** TX, OK, LA, FL, TN, IA, NV, WA, OR, MT, UT, WY, MS, plus BLM land
  in CO/NE/WY/SD. Add a state by writing a parser and a `FEEDS` entry in `server.py`.
- **Every state + DC:** `static/states.json` links each state's official forestry/fire agency, shown
  in the verdict card and the "Official source for every state" list.
- **Fire weather alerts:** National Weather Service (Red Flag Warnings, Fire Weather Watches).
- **Community reports:** saved to `data/reports.json` for states with no feed.
- Map tiles and place search: OpenStreetMap.

A missing ban is not proof there isn't one: cities, fire districts and federal land set their own rules.
