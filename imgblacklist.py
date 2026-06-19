import discord
from discord import app_commands
import json
import os
import httpx
import imagehash
from PIL import Image
import io
import asyncio
import time
from collections import defaultdict, deque
from dotenv import load_dotenv
from typing import Literal, TypedDict

# ==================== Config ====================

load_dotenv()
TOKEN = os.getenv('DISCORD_TOKEN')

BLACKLIST_FILE = 'blacklist.json'
CONFIG_FILE    = 'config.json'

MAX_IMAGE_BYTES = 10 * 1024 * 1024  # 10 MB


# ==================== Types ====================

class BlacklistEntry(TypedDict):
    reason:   str
    rule:     str
    added_by: str
    added_at: int


class BotConfig(TypedDict):
    scan_channels:      list[int]
    log_channels:       dict[str, int]
    threshold:          int
    flood_window:       float
    flood_threshold:    int
    hardkill_reasons:   list[str]
    hardkill_threshold: float


DEFAULT_CONFIG: BotConfig = {
    "scan_channels":      [],
    "log_channels":       {},
    "threshold":          12,
    "flood_window":       10.0,
    "flood_threshold":    5,
    "hardkill_reasons":   [],
    "hardkill_threshold": 90.0,
}


# ==================== Memory cache ====================

class BotCache:
    """Central in-memory state. All attributes are typed."""

    def __init__(self):
        # key: compiled imagehash object  →  value: BlacklistEntry
        self.blacklist: dict[imagehash.ImageHash, BlacklistEntry] = {}
        self.config: BotConfig = {}

    def load(self):
        raw_bl  = _load_json(BLACKLIST_FILE)
        raw_cfg = _load_json(CONFIG_FILE)

        # Compile hash strings → ImageHash objects
        compiled: dict[imagehash.ImageHash, BlacklistEntry] = {}
        for h_str, info in raw_bl.items():
            try:
                compiled[imagehash.hex_to_hash(h_str)] = info
            except Exception as e:
                print(f"[cache] Failed to compile hash '{h_str}': {e}")
        self.blacklist = compiled

        # Merge loaded config with defaults so missing keys are always present
        cfg: BotConfig = {**DEFAULT_CONFIG, **raw_cfg}
        self.config = cfg

    def raw_blacklist(self) -> dict[str, BlacklistEntry]:
        """Return blacklist as {hex_string: entry} for JSON serialisation."""
        return {str(h): info for h, info in self.blacklist.items()}


cache = BotCache()
_file_lock = asyncio.Lock()


# ==================== File helpers ====================

def _get_path(filename: str) -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), filename)


def _load_json(filename: str) -> dict:
    path = _get_path(filename)
    if not os.path.exists(path):
        return {}
    with open(path, 'r', encoding='utf-8') as f:
        try:
            return json.load(f)
        except json.JSONDecodeError:
            return {}


async def _save_json(filename: str, data: dict) -> None:
    async with _file_lock:
        def _write():
            with open(_get_path(filename), 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=4, ensure_ascii=False)
        await asyncio.to_thread(_write)


# ==================== Image helpers ====================

def _compute_hash(image_bytes: bytes) -> imagehash.ImageHash:
    return imagehash.phash(Image.open(io.BytesIO(image_bytes)))


def _check_blacklist(
    image_bytes: bytes,
    blacklist: dict[imagehash.ImageHash, BlacklistEntry],
    threshold: int,
) -> tuple[bool, str | None, str | None, float]:
    """
    Compare image against the blacklist.

    Returns:
        (matched, rule_id, reason, best_similarity_pct)
    """
    try:
        current_hash  = _compute_hash(image_bytes)
        best_sim      = 0.0
        match_rule    = None
        match_reason  = None
        found         = False

        for stored_hash, info in blacklist.items():
            diff = current_hash - stored_hash
            sim  = max(0.0, (1.0 - diff / 64.0) * 100.0)

            if diff <= threshold:
                found        = True
                match_rule   = info.get("rule",   "3")
                match_reason = info.get("reason", "Blacklisted Image")
                best_sim     = sim
                break

            if sim > best_sim:
                best_sim = sim

        return found, match_rule, match_reason, round(best_sim, 1)

    except Exception as e:
        print(f"[hash] Comparison error: {e}")
        return False, None, None, 0.0


async def _download_image(client: httpx.AsyncClient, url: str) -> bytes | None:
    """
    Download an image with a hard size cap (MAX_IMAGE_BYTES).
    Returns raw bytes, or None if the request fails or the file is too large.
    """
    try:
        async with client.stream("GET", url) as resp:
            if resp.status_code != 200:
                return None
            chunks, total = [], 0
            async for chunk in resp.aiter_bytes(8192):
                total += len(chunk)
                if total > MAX_IMAGE_BYTES:
                    print(f"[download] Rejected oversized image from {url}")
                    return None
                chunks.append(chunk)
            return b"".join(chunks)
    except Exception as e:
        print(f"[download] Error fetching {url}: {e}")
        return None


def _is_image(filename: str) -> bool:
    return filename.lower().endswith(('.png', '.jpg', '.jpeg', '.webp'))


# ==================== UI components ====================

class LogActionView(discord.ui.View):
    def __init__(self, original_msg: discord.Message):
        super().__init__(timeout=None)
        self.original_msg = original_msg
        self.add_item(discord.ui.Button(label="Jump to Message", url=original_msg.jump_url))

    @discord.ui.button(label="Delete Message", style=discord.ButtonStyle.danger)
    async def delete_msg(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.user.guild_permissions.manage_messages:
            return await interaction.response.send_message(
                "You do not have permission to manage messages.", ephemeral=True
            )
        try:
            await self.original_msg.delete()

            embed = interaction.message.embeds[0].copy()
            embed.title = "Blacklist Image Detected"
            embed.color = discord.Color.greyple()
            embed.add_field(name="Status",    value="Message Deleted",         inline=True)
            embed.add_field(name="Moderator", value=interaction.user.mention,  inline=True)

            button.disabled = True
            button.label    = "Deleted"
            button.style    = discord.ButtonStyle.secondary

            new_view = discord.ui.View(timeout=None)
            new_view.add_item(button)
            await interaction.response.edit_message(embed=embed, view=new_view)

        except discord.NotFound:
            await interaction.response.send_message("Message already deleted.", ephemeral=True)
        except Exception as e:
            await interaction.response.send_message(f"Error: {e}", ephemeral=True)


class TestActionView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=60)

    @discord.ui.button(label="Ignore", style=discord.ButtonStyle.secondary)
    async def cancel_action(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="Test ignored.", view=None)


# ==================== Bot ====================

class HashBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(intents=intents)
        self.tree         = app_commands.CommandTree(self)
        self.http_client: httpx.AsyncClient | None = None

        # Per-user upload timestamps for flood detection
        self.upload_tracker: dict[int, deque[float]] = defaultdict(
            lambda: deque(maxlen=200)
        )

        # Pending hardkill log entries batched by (guild_id, user_id)
        # Swapped atomically in the background sender to avoid race conditions
        self._log_queue: dict[tuple[int, int], list[dict]] = {}

    # ---------- lifecycle ----------

    async def setup_hook(self):
        self.http_client = httpx.AsyncClient()
        cache.load()
        self.loop.create_task(self._background_log_sender())
        await self.tree.sync()

    async def on_ready(self):
        print(f"Logged in as {self.user}")

    async def close(self):
        if self.http_client:
            await self.http_client.aclose()
        await super().close()

    # ---------- background tasks ----------

    async def _background_log_sender(self):
        """
        Drain the log queue every 5 seconds and send batched embeds to log channels.
        Uses an atomic swap so entries appended during processing are never lost.
        """
        await self.wait_until_ready()
        while not self.is_closed():
            await asyncio.sleep(5.0)

            if not self._log_queue:
                continue

            # Atomic swap: take the current queue and replace with a fresh dict.
            # Any entries appended between now and the next iteration land in the
            # new dict and are processed in the following cycle.
            current_queue, self._log_queue = self._log_queue, {}

            for (guild_id, user_id), logs in current_queue.items():
                log_ch_id = cache.config["log_channels"].get(str(guild_id))
                if not log_ch_id:
                    continue
                log_ch = self.get_channel(log_ch_id)
                if not log_ch:
                    continue

                first    = logs[0]
                count    = len(logs)
                hk_thr   = cache.config["hardkill_threshold"]

                unique_rules   = {f"Violation of Server Rule #{l['rule_id']}" for l in logs}
                unique_reasons = {l["reason"] for l in logs}

                embed = discord.Embed(title="Message Deleted", color=discord.Color.dark_red())
                embed.add_field(name="User",        value=f"{first['author'].mention} (`{user_id}`)", inline=True)
                embed.add_field(name="Channel",     value=first["channel"].mention,                   inline=True)
                embed.add_field(name="Similarity",  value=f"`{first['similarity']}%`",                inline=True)
                embed.add_field(name="Reason",      value="\n".join(unique_rules),                    inline=False)
                embed.add_field(name="Description", value="\n".join(unique_reasons),                  inline=False)
                embed.add_field(
                    name="Status",
                    value=f"Automatically Deleted (Hardkill @ {hk_thr}%+)",
                    inline=False,
                )

                file = discord.File(io.BytesIO(first["image"]), filename="violation.png")
                embed.set_image(url="attachment://violation.png")
                embed.set_footer(
                    text=f"{'Batch processed | ' if count > 1 else ''}Total {count} image(s) deleted."
                )

                try:
                    await log_ch.send(embed=embed, file=file)
                except Exception as e:
                    print(f"[log_sender] Failed to send batch log: {e}")

    # ---------- flood detection ----------

    async def _check_flood(self, message: discord.Message, new_image_count: int) -> bool:
        """
        Track per-user image uploads within a rolling time window.
        Returns True if the flood threshold has been exceeded (caller should stop further processing).
        """
        now       = time.time()
        user_id   = message.author.id
        window    = cache.config["flood_window"]
        threshold = cache.config["flood_threshold"]

        dq = self.upload_tracker[user_id]

        # Evict expired timestamps
        while dq and now - dq[0] >= window:
            dq.popleft()

        for _ in range(new_image_count):
            dq.append(now)

        if len(dq) < threshold:
            return False

        # Flood triggered — alert and reset tracker for this user
        log_ch_id = cache.config["log_channels"].get(str(message.guild.id))
        if log_ch_id:
            log_ch = self.get_channel(log_ch_id)
            if log_ch:
                embed = discord.Embed(title="Mass Image Upload Detected", color=discord.Color.orange())
                embed.add_field(name="User",      value=f"{message.author.mention} (`{user_id}`)", inline=True)
                embed.add_field(name="Channel",   value=message.channel.mention,                   inline=True)
                embed.add_field(name="Detection", value=f"{len(dq)} images in {window}s",           inline=True)
                embed.set_footer(text="User exceeded the image upload limit within the time window.")
                try:
                    await log_ch.send(embed=embed)
                except Exception as e:
                    print(f"[flood] Failed to send alert: {e}")

        del self.upload_tracker[user_id]
        return True

    # ---------- image scanning ----------

    async def _scan_images(self, message: discord.Message, attachments: list[discord.Attachment]):
        """Download and hash-check each attachment; handle soft/hardkill outcomes."""
        scan_threshold = cache.config["threshold"]
        hk_reasons     = cache.config["hardkill_reasons"]
        hk_threshold   = cache.config["hardkill_threshold"]
        log_ch_id      = cache.config["log_channels"].get(str(message.guild.id))

        for att in attachments:
            image_bytes = await _download_image(self.http_client, att.url)
            if image_bytes is None:
                continue

            try:
                matched, rule_id, reason, similarity = await asyncio.to_thread(
                    _check_blacklist, image_bytes, cache.blacklist, scan_threshold
                )
            except Exception as e:
                print(f"[scan] Hash error for {att.filename}: {e}")
                continue

            if not matched:
                continue

            is_hardkill = (reason in hk_reasons) and (similarity >= hk_threshold)

            if is_hardkill:
                try:
                    await message.delete()
                except discord.HTTPException as e:
                    print(f"[scan] Could not delete message {message.id}: {e}")

                key = (message.guild.id, message.author.id)
                self._log_queue.setdefault(key, []).append({
                    "author":     message.author,
                    "channel":    message.channel,
                    "rule_id":    rule_id,
                    "reason":     reason,
                    "similarity": similarity,
                    "image":      image_bytes,
                })
                break  # One hardkill per message is enough

            else:
                if not log_ch_id:
                    continue
                log_ch = self.get_channel(log_ch_id)
                if not log_ch:
                    continue

                embed = discord.Embed(title="Blacklist Image Detected", color=0xFF4B4B)
                embed.add_field(name="User",        value=f"{message.author.mention} (`{message.author.id}`)")
                embed.add_field(name="Channel",     value=message.channel.mention)
                embed.add_field(name="Similarity",  value=f"`{similarity}%`",                     inline=True)
                embed.add_field(name="Reason",      value=f"Violation of Server Rule #{rule_id}", inline=True)
                embed.add_field(name="Description", value=reason,                                 inline=False)
                embed.set_image(url=att.url)

                try:
                    await log_ch.send(embed=embed, view=LogActionView(message))
                except Exception as e:
                    print(f"[scan] Failed to send log: {e}")

    # ---------- event ----------

    async def on_message(self, message: discord.Message):
        if message.author.id == self.user.id:
            return
        if message.channel.id not in cache.config["scan_channels"]:
            return

        image_attachments = [a for a in message.attachments if _is_image(a.filename)]
        if not image_attachments:
            return

        if await self._check_flood(message, len(image_attachments)):
            return

        await self._scan_images(message, image_attachments)


bot = HashBot()


# ==================== Context menu ====================

@bot.tree.context_menu(name="Check Image Hash")
@app_commands.default_permissions(manage_messages=True)
async def check_hash_only(interaction: discord.Interaction, message: discord.Message):
    image_attachments = [a for a in message.attachments if _is_image(a.filename)]
    if not image_attachments:
        return await interaction.response.send_message(
            "No supported images found in this message.", ephemeral=True
        )

    await interaction.response.defer(ephemeral=True)

    image_bytes = await _download_image(bot.http_client, image_attachments[0].url)
    if image_bytes is None:
        return await interaction.followup.send("Failed to download image.", ephemeral=True)

    try:
        scan_threshold = cache.config["threshold"]
        matched, rule_id, reason, similarity = await asyncio.to_thread(
            _check_blacklist, image_bytes, cache.blacklist, scan_threshold
        )
    except Exception as e:
        return await interaction.followup.send(f"Error during hash check: {e}", ephemeral=True)

    if matched:
        embed = discord.Embed(title="Blacklist Match Found", color=0xFF4B4B)
        embed.add_field(name="Rule ID",    value=f"#{rule_id}",       inline=True)
        embed.add_field(name="Similarity", value=f"`{similarity}%`",  inline=True)
        embed.add_field(name="Reason",     value=reason,              inline=False)
        embed.set_thumbnail(url=image_attachments[0].url)
        await interaction.followup.send(embed=embed, view=TestActionView(), ephemeral=True)
    else:
        await interaction.followup.send(
            f"No match found.\n(Best similarity: `{similarity}%` | Threshold: `{scan_threshold}`)",
            ephemeral=True,
        )


# ==================== /blacklist ====================

@bot.tree.command(name="blacklist", description="Manage the image blacklist")
@app_commands.default_permissions(manage_messages=True)
@app_commands.describe(
    action=    "add / remove / list / show",
    message_id="Message ID containing the image (required for: add)",
    reason=    "Violation description (required for: add)",
    rule=      "Rule number/ID (required for: add)",
    number=    "Item index from /blacklist list (required for: remove, show)",
)
async def blacklist_cmd(
    interaction: discord.Interaction,
    action:     Literal["add", "remove", "list", "show"],
    message_id: str = None,
    reason:     str = None,
    rule:       str = None,
    number:     int = None,
):
    # ---- add ----
    if action == "add":
        if not (message_id and reason and rule):
            return await interaction.response.send_message(
                "**Error:** `message_id`, `reason`, and `rule` are all required for `add`.",
                ephemeral=True,
            )

        await interaction.response.defer(ephemeral=True)

        try:
            target_msg = await interaction.channel.fetch_message(int(message_id))
        except (discord.NotFound, discord.Forbidden, ValueError):
            return await interaction.followup.send(
                "**Error:** Message not found in this channel or invalid ID.", ephemeral=True
            )

        images = [a for a in target_msg.attachments if _is_image(a.filename)]
        if not images:
            return await interaction.followup.send(
                "**Error:** No supported image attachments in that message.", ephemeral=True
            )

        image_bytes = await _download_image(bot.http_client, images[0].url)
        if image_bytes is None:
            return await interaction.followup.send(
                "**Error:** Failed to download the image.", ephemeral=True
            )

        try:
            computed_hash = await asyncio.to_thread(_compute_hash, image_bytes)
            hash_str      = str(computed_hash)

            entry: BlacklistEntry = {
                "reason":   reason,
                "rule":     rule,
                "added_by": str(interaction.user.id),
                "added_at": int(time.time()),
            }

            cache.blacklist[computed_hash] = entry
            await _save_json(BLACKLIST_FILE, cache.raw_blacklist())

            embed = discord.Embed(title="Image Added to Blacklist", color=discord.Color.green())
            embed.add_field(name="Hash",   value=f"`{hash_str}`", inline=False)
            embed.add_field(name="Rule",   value=rule,            inline=True)
            embed.add_field(name="Reason", value=reason,          inline=True)
            embed.set_thumbnail(url=images[0].url)
            await interaction.followup.send(embed=embed, ephemeral=True)

        except Exception as e:
            await interaction.followup.send(f"**Error processing image:** {e}", ephemeral=True)

    # ---- list ----
    elif action == "list":
        raw = cache.raw_blacklist()
        if not raw:
            return await interaction.response.send_message(
                "The blacklist is currently empty.", ephemeral=True
            )

        lines = [
            f"**#{i}** | `{h}` | Rule: `{info.get('rule')}` | {info.get('reason')}"
            for i, (h, info) in enumerate(raw.items(), start=1)
        ]
        text  = "\n".join(lines)
        embed = discord.Embed(title="Blacklist", color=discord.Color.blue())
        embed.description = text[:3950] + "\n\n*...truncated*" if len(text) > 4000 else text
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ---- remove / show ----
    elif action in ("remove", "show"):
        if number is None:
            return await interaction.response.send_message(
                f"**Error:** `number` is required for `{action}`.", ephemeral=True
            )

        raw  = cache.raw_blacklist()
        keys = list(raw.keys())

        if not keys:
            return await interaction.response.send_message(
                "The blacklist is currently empty.", ephemeral=True
            )
        if not (1 <= number <= len(keys)):
            return await interaction.response.send_message(
                f"**Error:** Choose a number between 1 and {len(keys)}.", ephemeral=True
            )

        target_str  = keys[number - 1]
        target_info = raw[target_str]

        if action == "show":
            embed = discord.Embed(
                title=f"Blacklist Item #{number}", color=discord.Color.dark_orange()
            )
            embed.add_field(name="Hash",     value=f"`{target_str}`",                                      inline=False)
            embed.add_field(name="Rule",     value=target_info.get("rule"),                                inline=True)
            embed.add_field(name="Reason",   value=target_info.get("reason"),                              inline=True)
            embed.add_field(name="Added By", value=f"<@{target_info.get('added_by')}> (`{target_info.get('added_by')}`)", inline=False)
            embed.add_field(name="Added At", value=f"<t:{target_info.get('added_at')}:F>",                 inline=False)
            await interaction.response.send_message(embed=embed, ephemeral=True)

        else:  # remove
            try:
                del cache.blacklist[imagehash.hex_to_hash(target_str)]
            except Exception as e:
                print(f"[blacklist] Cache key removal error: {e}")

            await _save_json(BLACKLIST_FILE, cache.raw_blacklist())
            await interaction.response.send_message(
                f"🗑️ Item **#{number}** (`{target_str}`) removed.", ephemeral=True
            )


# ==================== /scan ====================

@bot.tree.command(name="scan", description="Start or stop auto-scanning in a channel")
@app_commands.default_permissions(manage_messages=True)
@app_commands.describe(action="start or stop", channel="Target channel")
async def scan_cmd(
    interaction: discord.Interaction,
    action:  Literal["start", "stop"],
    channel: discord.TextChannel,
):
    scan_list = cache.config["scan_channels"]

    if action == "start":
        if channel.id not in scan_list:
            scan_list.append(channel.id)
            status = f"Started auto-scanning in {channel.mention}."
        else:
            status = f"{channel.mention} is already being scanned."
    else:
        if channel.id in scan_list:
            scan_list.remove(channel.id)
            status = f"Stopped auto-scanning in {channel.mention}."
        else:
            status = f"{channel.mention} was not being scanned."

    cache.config["scan_channels"] = scan_list
    await _save_json(CONFIG_FILE, cache.config)
    await interaction.response.send_message(status, ephemeral=True)


# ==================== /log ====================

@bot.tree.command(name="log", description="Set a channel for violation logs")
@app_commands.default_permissions(manage_messages=True)
@app_commands.describe(channel="Channel to send logs to")
async def set_log(interaction: discord.Interaction, channel: discord.TextChannel):
    cache.config["log_channels"][str(interaction.guild.id)] = channel.id
    await _save_json(CONFIG_FILE, cache.config)
    await interaction.response.send_message(
        f"Log channel set to {channel.mention}.", ephemeral=True
    )


# ==================== /set_threshold ====================

@bot.tree.command(name="set_threshold", description="Set the global image matching threshold")
@app_commands.default_permissions(manage_messages=True)
@app_commands.describe(value="Hash distance threshold (lower = stricter, default 12)")
async def set_threshold(interaction: discord.Interaction, value: int):
    if value < 0:
        return await interaction.response.send_message(
            "Threshold must be 0 or greater.", ephemeral=True
        )
    cache.config["threshold"] = value
    await _save_json(CONFIG_FILE, cache.config)
    await interaction.response.send_message(
        f"Global detection threshold set to `{value}`.", ephemeral=True
    )


# ==================== /set_flood ====================

@bot.tree.command(name="set_flood", description="Configure mass image upload detection")
@app_commands.default_permissions(manage_messages=True)
@app_commands.describe(
    duration="Rolling time window in seconds (e.g. 10)",
    count=   "Max images allowed within that window (e.g. 5)",
)
async def set_flood(interaction: discord.Interaction, duration: float, count: int):
    if duration <= 0 or count <= 0:
        return await interaction.response.send_message(
            "Duration and count must each be greater than 0.", ephemeral=True
        )
    cache.config["flood_window"]    = duration
    cache.config["flood_threshold"] = count
    await _save_json(CONFIG_FILE, cache.config)
    await interaction.response.send_message(
        f"Flood detection: **{count}** images within **{duration}s**.", ephemeral=True
    )


# ==================== /hardkill ====================

@bot.tree.command(name="hardkill", description="Manage automatic deletion rules")
@app_commands.default_permissions(manage_messages=True)
@app_commands.describe(
    action="add / remove / list / threshold",
    reason="Exact reason string (required for: add, remove)",
    value= "Similarity 0–100 (required for: threshold)",
)
async def hardkill_cmd(
    interaction: discord.Interaction,
    action: Literal["add", "remove", "list", "threshold"],
    reason: str   = None,
    value:  float = None,
):
    hk_list = cache.config["hardkill_reasons"]

    if action == "list":
        hk_thr    = cache.config["hardkill_threshold"]
        body      = "\n".join(f"- {r}" for r in hk_list) if hk_list else "No reasons registered."
        return await interaction.response.send_message(
            f"### Hardkill Settings\n- **Min Similarity:** `{hk_thr}%`\n\n**Reasons:**\n{body}",
            ephemeral=True,
        )

    if action == "threshold":
        if value is None or not (0 <= value <= 100):
            return await interaction.response.send_message(
                "Provide a value between 0 and 100.", ephemeral=True
            )
        cache.config["hardkill_threshold"] = value
        await _save_json(CONFIG_FILE, cache.config)
        return await interaction.response.send_message(
            f"Hardkill threshold set to `{value}%`.", ephemeral=True
        )

    if not reason:
        return await interaction.response.send_message(
            "Provide a reason string.", ephemeral=True
        )

    if action == "add":
        if reason not in hk_list:
            hk_list.append(reason)
            status = f"Added `{reason}` to hardkill list."
        else:
            status = f"`{reason}` is already in the list."
    else:  # remove
        if reason in hk_list:
            hk_list.remove(reason)
            status = f"Removed `{reason}` from hardkill list."
        else:
            status = f"`{reason}` not found in the list."

    cache.config["hardkill_reasons"] = hk_list
    await _save_json(CONFIG_FILE, cache.config)
    await interaction.response.send_message(status, ephemeral=True)


# ==================== Entry point ====================

if TOKEN:
    bot.run(TOKEN)
  
