import os
from pymongo import MongoClient, UpdateOne
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

MONGO_URI = os.getenv("MONGO_URI")
DB_NAME = os.getenv("MONGO_DB", "clashdb")

if not MONGO_URI:
    raise ValueError("MONGO_URI not found in .env file")

client = MongoClient(MONGO_URI)
db = client[DB_NAME]

fixtures_col = db["games"]
channel_fixtures_col = db["channel_fixtures"]

print("=" * 60)
print("MIGRATION: fixtures → channel_fixtures")
print("=" * 60)

# Fields to REMOVE from fixtures
FIELDS_TO_REMOVE_FROM_FIXTURES = [
    "votes",
    "voters",
    "comments",
    "commentary",
    "commentaryCount",
    "pledges",
    "bets",
    "likes",
]

# Fields to ADD to channel_fixtures (with defaults)
FIELDS_TO_ADD_TO_CHANNEL = {
    "vote_counts": {"home": 0, "away": 0, "draw": 0},
    "comment_count": 0,
    "pledge_count": 0,
    "bet_count": 0,
    "likes_count": 0,
    "last_message": "",
    "last_message_at": None,
    "last_sender": "",
    "unread_counts": {},
}

print("\n📊 Fetching all fixtures...")
fixtures = fixtures_col.find({})

channel_updates = []
fixture_updates = []
updated_channels = 0

for fixture in fixtures:
    fixture_id = fixture.get("match_id") or fixture.get("_id")
    if not fixture_id:
        continue

    # Find all channel_fixtures for this fixture
    channel_fixtures = channel_fixtures_col.find({"fixture_id": fixture_id})

    for cf in channel_fixtures:
        update_doc = {}

        # Add fields if they don't exist
        for field, default_value in FIELDS_TO_ADD_TO_CHANNEL.items():
            if field not in cf or cf.get(field) is None:
                if field == "vote_counts":
                    # Special handling for vote_counts
                    total_votes = fixture.get("votes", 0)
                    if total_votes > 0:
                        update_doc["vote_counts"] = {
                            "home": total_votes,
                            "away": 0,
                            "draw": 0,
                        }
                    else:
                        update_doc["vote_counts"] = {"home": 0, "away": 0, "draw": 0}
                elif field == "comment_count":
                    comments = fixture.get("comments", 0)
                    commentary_count = fixture.get("commentaryCount", 0)
                    update_doc["comment_count"] = comments + commentary_count
                else:
                    # Use fixture value if available, else default
                    fixture_value = fixture.get(field.replace("_count", "s"), None)
                    if fixture_value is not None:
                        update_doc[field] = fixture_value
                    else:
                        update_doc[field] = default_value

        if update_doc:
            channel_updates.append(UpdateOne({"_id": cf["_id"]}, {"$set": update_doc}))
            updated_channels += 1
            print(f"  ✅ Updated channel_fixture for {fixture_id}")

    # Build remove operation for this fixture
    remove_doc = {}
    for field in FIELDS_TO_REMOVE_FROM_FIXTURES:
        if field in fixture:
            remove_doc[field] = ""

    if remove_doc:
        fixture_updates.append(
            UpdateOne({"_id": fixture["_id"]}, {"$unset": remove_doc})
        )
        print(f"  🗑️  Removed fields from fixture {fixture_id}")

# Execute channel_fixtures updates
if channel_updates:
    print(f"\n📤 Updating {len(channel_updates)} channel_fixtures...")
    result = channel_fixtures_col.bulk_write(channel_updates)
    print(f"✅ Updated {result.modified_count} channel_fixtures")

# Execute fixture updates (REMOVE fields)
if fixture_updates:
    print(f"\n🗑️  Removing fields from {len(fixture_updates)} fixtures...")
    result = fixtures_col.bulk_write(fixture_updates)
    print(f"✅ Removed fields from {result.modified_count} fixtures")

# ================================================================
# VERIFICATION
# ================================================================

print("\n" + "=" * 60)
print("VERIFICATION")
print("=" * 60)

# Check a fixture to verify fields are removed
sample_fixture = fixtures_col.find_one({})
if sample_fixture:
    fixture_id = sample_fixture.get("match_id") or sample_fixture.get("_id")
    print(f"\n📋 Fixture {fixture_id} after migration:")
    print(f"   - Has 'votes'? {'votes' in sample_fixture}")
    print(f"   - Has 'pledges'? {'pledges' in sample_fixture}")
    print(f"   - Has 'bets'? {'bets' in sample_fixture}")
    print(f"   - Has 'comments'? {'comments' in sample_fixture}")

# Check a channel_fixture to verify fields are added
sample_cf = channel_fixtures_col.find_one({})
if sample_cf:
    print(f"\n📋 Channel Fixture after migration:")
    print(f"   - vote_counts: {sample_cf.get('vote_counts', 'NOT FOUND')}")
    print(f"   - comment_count: {sample_cf.get('comment_count', 'NOT FOUND')}")
    print(f"   - pledge_count: {sample_cf.get('pledge_count', 'NOT FOUND')}")
    print(f"   - bet_count: {sample_cf.get('bet_count', 'NOT FOUND')}")
    print(f"   - likes_count: {sample_cf.get('likes_count', 'NOT FOUND')}")
    print(f"   - last_message: '{sample_cf.get('last_message', 'NOT FOUND')}'")

print("\n" + "=" * 60)
print("SUMMARY")
print("=" * 60)
print(f"✅ Channel fixtures updated: {updated_channels}")
print(f"🗑️  Fixtures cleaned: {len(fixture_updates)}")
print("✅ Migration complete!")

client.close()
