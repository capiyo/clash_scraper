# games.rs patch

Two handlers use `cursor.try_collect()`, which aborts the ENTIRE query on the
first bad document. `get_games` already does the right thing (skip-and-log).
Apply the same pattern to `get_live_games` and `get_upcoming_games` so one
corrupted fixture can never take down the whole live/upcoming feed again.

## get_live_games — replace with:

```rust
pub async fn get_live_games(State(state): State<AppState>) -> Result<Json<Vec<Game>>> {
    let collection: Collection<Game> = state.db.collection("fixtures");
    let filter = doc! { "status": "live", "isLive": true };

    let mut cursor = collection.find(filter).await?;
    let mut live_games: Vec<Game> = Vec::new();
    let mut skipped = 0;

    while cursor.advance().await? {
        match cursor.deserialize_current() {
            Ok(game) => live_games.push(game),
            Err(e) => {
                skipped += 1;
                tracing::error!("⚠️ Skipping malformed live fixture document: {}", e);
            }
        }
    }

    if skipped > 0 {
        tracing::warn!("⚠️ Skipped {} malformed live fixture document(s)", skipped);
    }

    tracing::info!("✅ Fetched {} live games ({} skipped)", live_games.len(), skipped);
    Ok(Json(live_games))
}
```

## get_upcoming_games — replace the fetch section with:

```rust
pub async fn get_upcoming_games(State(state): State<AppState>) -> Result<Json<Vec<Game>>> {
    let collection: Collection<Game> = state.db.collection("fixtures");
    let filter = doc! { "status": "upcoming" };

    let mut cursor = collection.find(filter).await?;
    let mut games: Vec<Game> = Vec::new();
    let mut skipped = 0;

    while cursor.advance().await? {
        match cursor.deserialize_current() {
            Ok(game) => games.push(game),
            Err(e) => {
                skipped += 1;
                tracing::error!("⚠️ Skipping malformed upcoming fixture document: {}", e);
            }
        }
    }

    if skipped > 0 {
        tracing::warn!("⚠️ Skipped {} malformed upcoming fixture document(s)", skipped);
    }

    // --- rest of function (now_estimate / sorting into not_started vs
    // likely_over) is unchanged, just operates on the `games` Vec built
    // above instead of the old try_collect() result ---

    let now = Utc::now();
    const MATCH_DURATION_MINS: i64 = 120;

    let mut not_started: Vec<Game> = Vec::new();
    let mut likely_over: Vec<Game> = Vec::new();

    for game in games {
        match parse_kickoff_utc(&game.date_iso, &game.time) {
            Some(kickoff) => {
                let end_estimate = kickoff + chrono::Duration::minutes(MATCH_DURATION_MINS);
                if end_estimate < now {
                    likely_over.push(game);
                } else {
                    not_started.push(game);
                }
            }
            None => not_started.push(game),
        }
    }

    not_started.sort_by(|a, b| {
        let ka = format!("{} {}", a.date_iso, a.time);
        let kb = format!("{} {}", b.date_iso, b.time);
        ka.cmp(&kb)
    });

    likely_over.sort_by(|a, b| {
        let ka = format!("{} {}", a.date_iso, a.time);
        let kb = format!("{} {}", b.date_iso, b.time);
        kb.cmp(&ka)
    });

    let mut sorted: Vec<Game> = not_started;
    sorted.extend(likely_over);

    tracing::info!(
        "✅ Returning {} upcoming games ({} skipped)",
        sorted.len(),
        skipped
    );
    Ok(Json(sorted))
}
```

Nothing else in games.rs needs to change — `get_games` was already correct,
and the model (`game.rs`) matches the corrected Python output once the
poller/mongo_store fixes are deployed and you rescrape.# Patch for sources/threesixtyfive.py

Only one change needed in the whole file. `fetch_lineups()` currently
hardcodes the World Cup match-id prefix:

```python
    result = {
        "fixture_id": f"wc26_{game_id}",
        "home": home_lineups or {},
        "away": away_lineups or {},
    }
```

Replace with a `match_id_prefix` parameter so callers (poller.py, scraper.py)
pass in the same competition-keyed prefix used in scraper.py's `match_id`
(e.g. `"nations_league"`, `"euro_qualifiers"`) instead of an implicit
World Cup assumption:

```python
def fetch_lineups(
    game_id: str,
    away_id: int,
    home_id: int,
    competition_id: int,
    match_id_prefix: str = "intl",
) -> Optional[Dict[str, Any]]:
    ...
    result = {
        "fixture_id": f"{match_id_prefix}_{game_id}",
        "home": home_lineups or {},
        "away": away_lineups or {},
    }
```

Everything else in the file (fetch_games_by_competition, fetch_game_details,
fetch_statistics, fetch_commentary, fetch_match_events, is_game_finished,
classify_match_phase) is competition-agnostic already -- it takes
competition_id / game_id as plain parameters and doesn't assume World Cup
anywhere else. No other changes needed there.

Wherever poller.py calls fetch_lineups (I don't have that file's contents,
only scraper.py/config.py/main.py were pulled), pass
`match_id_prefix=<the same key you used for that game's match_id>`, e.g.
using scraper.py's `_ID_TO_KEY` mapping against the game's `competitionId`.