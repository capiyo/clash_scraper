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

# ============================================================================
# COLLECTIONS
# ============================================================================

posts_col = db["posts"]
comments_col = db["comments"]

print("=" * 60)
print("MIGRATION: Add Firebase Image Fields + Video Fields + Reply Support")
print("=" * 60)

# ============================================================================
# FIELDS TO ADD TO POSTS
# ============================================================================

POST_FIELDS = {
    # Video fields
    "video_url": None,
    "video_thumbnail_url": None,
    "video_duration": None,  # seconds
    "video_size": None,  # bytes
    "firebase_public_id": None,  # Firebase storage path for video
    # ✅ NEW: Firebase Image fields
    "firebase_image_url": None,  # Firebase storage URL for image
    "firebase_image_public_id": None,  # Firebase storage path for image
    "post_type": "text",  # text, image, video, text_and_image, text_and_video
}

# ============================================================================
# FIELDS TO ADD TO COMMENTS
# ============================================================================

COMMENT_REPLY_FIELDS = {
    "parent_comment_id": None,
}

print("\n📊 Fetching all posts...")
posts = posts_col.find({})

post_updates = []
updated_posts = 0

for post in posts:
    post_id = post.get("_id")
    if not post_id:
        continue

    update_doc = {}

    # Add fields if they don't exist
    for field, default_value in POST_FIELDS.items():
        if field not in post or post.get(field) is None:
            # Handle post_type based on existing data
            if field == "post_type":
                has_image = post.get("image_url") is not None
                has_video = post.get("video_url") is not None
                has_caption = (
                    post.get("caption") is not None and post.get("caption") != ""
                )

                if has_video and has_caption:
                    update_doc["post_type"] = "text_and_video"
                elif has_video:
                    update_doc["post_type"] = "video"
                elif has_image and has_caption:
                    update_doc["post_type"] = "text_and_image"
                elif has_image:
                    update_doc["post_type"] = "image"
                elif has_caption:
                    update_doc["post_type"] = "text"
                else:
                    update_doc["post_type"] = "text"
            else:
                update_doc[field] = default_value

    if update_doc:
        post_updates.append(UpdateOne({"_id": post_id}, {"$set": update_doc}))
        updated_posts += 1
        if updated_posts % 100 == 0:
            print(f"  ✅ Processed {updated_posts} posts...")

# ============================================================================
# ADD PARENT_COMMENT_ID TO COMMENTS
# ============================================================================

print("\n📊 Fetching all comments...")
comments = comments_col.find({})

comment_updates = []
updated_comments = 0

for comment in comments:
    comment_id = comment.get("_id")
    if not comment_id:
        continue

    update_doc = {}

    # Add parent_comment_id if it doesn't exist
    if "parent_comment_id" not in comment:
        update_doc["parent_comment_id"] = None

    if update_doc:
        comment_updates.append(UpdateOne({"_id": comment_id}, {"$set": update_doc}))
        updated_comments += 1
        if updated_comments % 100 == 0:
            print(f"  ✅ Processed {updated_comments} comments...")

# ============================================================================
# EXECUTE UPDATES
# ============================================================================

# Execute post updates
if post_updates:
    print(f"\n📤 Updating {len(post_updates)} posts...")
    result = posts_col.bulk_write(post_updates)
    print(f"✅ Updated {result.modified_count} posts")

# Execute comment updates
if comment_updates:
    print(f"\n📤 Updating {len(comment_updates)} comments...")
    result = comments_col.bulk_write(comment_updates)
    print(f"✅ Updated {result.modified_count} comments")

# ============================================================================
# VERIFICATION
# ============================================================================

print("\n" + "=" * 60)
print("VERIFICATION")
print("=" * 60)

# Check a post to verify fields are added
sample_post = posts_col.find_one({})
if sample_post:
    print(f"\n📋 Post after migration:")
    print(f"   - video_url: {sample_post.get('video_url', 'NOT FOUND')}")
    print(
        f"   - video_thumbnail_url: {sample_post.get('video_thumbnail_url', 'NOT FOUND')}"
    )
    print(f"   - video_duration: {sample_post.get('video_duration', 'NOT FOUND')}")
    print(f"   - video_size: {sample_post.get('video_size', 'NOT FOUND')}")
    print(
        f"   - firebase_public_id: {sample_post.get('firebase_public_id', 'NOT FOUND')}"
    )
    print(
        f"   - ✅ firebase_image_url: {sample_post.get('firebase_image_url', 'NOT FOUND')}"
    )
    print(
        f"   - ✅ firebase_image_public_id: {sample_post.get('firebase_image_public_id', 'NOT FOUND')}"
    )
    print(f"   - post_type: {sample_post.get('post_type', 'NOT FOUND')}")

# Check a comment to verify parent_comment_id is added
sample_comment = comments_col.find_one({})
if sample_comment:
    print(f"\n📋 Comment after migration:")
    print(
        f"   - parent_comment_id: {sample_comment.get('parent_comment_id', 'NOT FOUND')}"
    )

# ============================================================================
# SPECIFIC CHECK FOR YOUR POST
# ============================================================================

print("\n" + "=" * 60)
print("SPECIFIC POST CHECK")
print("=" * 60)

# Check the specific post you mentioned
your_post = posts_col.find_one({"_id": "69d0f5d0f3375cc3e6aaaa7a"})
if your_post:
    print(f"\n📋 Your post (ID: 69d0f5d0f3375cc3e6aaaa7a):")
    print(f"   - image_url: {your_post.get('image_url', 'NOT FOUND')}")
    print(
        f"   - cloudinary_public_id: {your_post.get('cloudinary_public_id', 'NOT FOUND')}"
    )
    print(
        f"   - ✅ firebase_image_url: {your_post.get('firebase_image_url', 'NOT FOUND')}"
    )
    print(
        f"   - ✅ firebase_image_public_id: {your_post.get('firebase_image_public_id', 'NOT FOUND')}"
    )
    print(f"   - video_url: {your_post.get('video_url', 'NOT FOUND')}")
    print(
        f"   - firebase_public_id: {your_post.get('firebase_public_id', 'NOT FOUND')}"
    )
    print(f"   - post_type: {your_post.get('post_type', 'NOT FOUND')}")
else:
    print("\n⚠️ Post with ID 69d0f5d0f3375cc3e6aaaa7a not found")

# ============================================================================
# COUNT STATS
# ============================================================================

print("\n" + "=" * 60)
print("STATISTICS")
print("=" * 60)

total_posts = posts_col.count_documents({})
total_comments = comments_col.count_documents({})

posts_with_video = posts_col.count_documents({"video_url": {"$ne": None}})
posts_with_image = posts_col.count_documents({"image_url": {"$ne": None}})
posts_with_firebase_image = posts_col.count_documents(
    {"firebase_image_url": {"$ne": None}}
)

print(f"\n📊 Total Posts: {total_posts}")
print(f"   - With Video: {posts_with_video}")
print(f"   - With Image: {posts_with_image}")
print(f"   - With Firebase Image: {posts_with_firebase_image}")

print(f"\n📊 Total Comments: {total_comments}")
comments_with_parent = comments_col.count_documents(
    {"parent_comment_id": {"$ne": None}}
)
print(f"   - With parent_comment_id: {comments_with_parent}")

# ============================================================================
# SUMMARY
# ============================================================================

print("\n" + "=" * 60)
print("SUMMARY")
print("=" * 60)
print(f"✅ Posts updated: {updated_posts}")
print(f"✅ Comments updated: {updated_comments}")
print("\n✅ Migration complete!")
print("\n📝 New fields added:")
print("   - firebase_image_url (String)")
print("   - firebase_image_public_id (String)")
print("   - video_url (String)")
print("   - video_thumbnail_url (String)")
print("   - video_duration (Number)")
print("   - video_size (Number)")
print("   - firebase_public_id (String)")
print("   - parent_comment_id (String)")

client.close()
