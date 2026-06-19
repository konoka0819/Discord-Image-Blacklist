# HashBot

Real-time Discord image monitoring bot using Perceptual Hashing (pHash). Detects resized or compressed blacklist images.

# Core Features

Image similarity matching for modified files

Automated immediate deletion (Hardkill) and manual moderation via logs (Softkill)

Mass image upload detection and alerts

5-second log batching to prevent API rate limits

# Commands

/blacklist: Manage blacklist data (add, remove, list, show)

/scan: Enable or disable channel monitoring

/log: Set notification channel

/set_threshold: Adjust image matching strictness

/set_flood: Configure upload limits

/hardkill: Manage auto-deletion rules

Context Menu (Check Image Hash): Right-click manual check
