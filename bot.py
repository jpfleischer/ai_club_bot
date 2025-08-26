import os
import discord
from discord import app_commands
from discord.ext import commands
import psycopg2
from typing import Optional
import io
import openpyxl


# ------------------------------------------------------------------------
# Environment variables (set these in .env, which docker-compose will load):
#
# DISCORD_TOKEN=YOUR_DISCORD_TOKEN
# DB_HOST=db           # "db" is the service name in docker-compose
# DB_USER=botuser
# DB_PASS=botpass
# DB_NAME=points_db
# GUILD_ID=you need to right click the ai club server and click copy id with developer mode on
# ------------------------------------------------------------------------

DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN")

DB_HOST = os.environ.get("DB_HOST", "localhost")
DB_USER = os.environ.get("DB_USER", "postgres")
DB_PASS = os.environ.get("DB_PASS", "")
DB_NAME = os.environ.get("DB_NAME", "points_db")

raw_gid = os.environ.get("GUILD_ID")
GUILD_ID: Optional[int] = int(raw_gid) if raw_gid and raw_gid.isdigit() else None

# 1) Connect to Postgres
conn = psycopg2.connect(
    host=DB_HOST,
    user=DB_USER,
    password=DB_PASS,
    dbname=DB_NAME
)
conn.autocommit = True  # So we don't have to manually commit
cursor = conn.cursor()

# 2) Ensure the necessary tables exist (if you haven't used init.sql)
cursor.execute("""
CREATE TABLE IF NOT EXISTS points (
    id SERIAL PRIMARY KEY,
    member_name VARCHAR(50) UNIQUE NOT NULL,
    points FLOAT DEFAULT 0
);

CREATE TABLE IF NOT EXISTS history (
    id SERIAL PRIMARY KEY,
    member_name VARCHAR(50) NOT NULL,
    reason TEXT,
    points FLOAT,  -- We'll store the delta (can be +x or -x)
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
""")

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)


PURGE_COMMANDS = os.environ.get("PURGE_COMMANDS") == "1"  # default off

@bot.event
async def on_ready():
    print(f"We have logged in as {bot.user}")
    try:
        guild = discord.Object(id=GUILD_ID) if GUILD_ID else None

        if guild:
            # Make guild-scoped copies of your global commands (instant visibility)
            bot.tree.copy_global_to(guild=guild)
            synced = await bot.tree.sync(guild=guild)
            print(f"Synced to guild {GUILD_ID}: {[c.name for c in synced]}")
        else:
            synced = await bot.tree.sync()
            print(f"Synced globally: {[c.name for c in synced]}")

        # Debug: what do we have locally vs remotely?
        print("Local commands:", [c.qualified_name for c in bot.tree.get_commands()])
        remote = await bot.tree.fetch_commands(guild=guild) if guild else await bot.tree.fetch_commands()
        print("Remote commands now:", [c.name for c in remote])

    except Exception as e:
        print(f"Error syncing commands: {e}")


def _has_cabinet_role(interaction: discord.Interaction) -> bool:
    if interaction.guild is None:
        return False
    member = interaction.user  # discord.Member during guild interactions
    roles = getattr(member, "roles", []) or []
    return any("cabinet" in (r.name or "").lower() for r in roles)

def cabinet_only():
    """Use as @cabinet_only() on sensitive commands."""
    return app_commands.check(_has_cabinet_role)

async def _deny_ephemeral(interaction: discord.Interaction, msg: str = "You need a Cabinet role to run this command."):
    # Avoid double-responding if something else already did
    if interaction.response.is_done():
        try:
            await interaction.followup.send(msg, ephemeral=True)
        except Exception:
            pass
    else:
        await interaction.response.send_message(msg, ephemeral=True)


# --- ONE global error handler for all app commands ---
@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    # Permission (check) failures → quiet, friendly reply
    if isinstance(error, app_commands.CheckFailure):
        await _deny_ephemeral(interaction, "You need a Cabinet role to run this command.")
        return

    # Autocomplete errors or other expected issues you want to hide from users
    if isinstance(error, app_commands.CommandInvokeError):
        # You can introspect error.original if you want to branch
        pass

    # Fallback: log and show a generic message without leaking internals
    try:
        print(f"[appcmd error] {type(error).__name__}: {error}")
        if not interaction.response.is_done():
            await interaction.response.send_message("Something went wrong running that command.", ephemeral=True)
        else:
            await interaction.followup.send("Something went wrong running that command.", ephemeral=True)
    except Exception:
        # Avoid crashing the handler
        pass



async def member_autocomplete(
    interaction: discord.Interaction,
    current: str
) -> list[app_commands.Choice[str]]:
    """
    Return a list of up to 25 matching member names based on `current` partial input.
    """
    if not _has_cabinet_role(interaction):
        return []
    # For example, fetch from DB where member_name ILIKE '%current%'
    cursor.execute("""
        SELECT member_name
        FROM points
        WHERE member_name ILIKE %s
        ORDER BY member_name
        LIMIT 25
    """, (f"%{current}%",))
    rows = cursor.fetchall()

    return [
        app_commands.Choice(name=r[0], value=r[0]) for r in rows
    ]


@cabinet_only()
@bot.tree.command(name="addmember", description="Add a new member to the database with 0 starting points.")
@app_commands.describe(member_name="The name/surname of the member to add.")
async def addmember(interaction: discord.Interaction, member_name: str):
    """
    Adds a new member to the 'points' table with 0 points (if not existing).
    """
    cursor.execute(
        "SELECT member_name FROM points WHERE member_name = %s", (member_name,))
    row = cursor.fetchone()
    if row is not None:
        await interaction.response.send_message(
            f"Member '{member_name}' already exists in the database!",
            ephemeral=True
        )
        return

    cursor.execute(
        "INSERT INTO points (member_name, points) VALUES (%s, %s)",
        (member_name, 0.0)
    )
    await interaction.response.send_message(
        f"Member '{member_name}' added with 0 points."
    )


@cabinet_only()
@bot.tree.command(name="removemember", description="Remove a member and their history from the database.")
@app_commands.describe(member="The name/surname of the member to remove from the database.")
@app_commands.autocomplete(member=member_autocomplete)
async def removemember(interaction: discord.Interaction, member: str):
    """
    Removes the specified member from the 'points' table
    and deletes all corresponding logs from the 'history' table.
    """
    # 1) Check if member exists
    cursor.execute(
        "SELECT member_name FROM points WHERE member_name = %s", (member,))
    row = cursor.fetchone()
    if row is None:
        await interaction.response.send_message(
            f"Member '{member}' does not exist in the database!",
            ephemeral=True
        )
        return

    # 2) Delete rows from 'history' first (optional but usually desired)
    cursor.execute("DELETE FROM history WHERE member_name = %s", (member,))

    # 3) Remove from 'points'
    cursor.execute("DELETE FROM points WHERE member_name = %s", (member,))

    # 4) Confirmation
    await interaction.response.send_message(
        f"Member '{member}' has been removed from the database (including all history)."
    )


@cabinet_only()
@bot.tree.command(name="addpoints", description="Add points to an existing member, with a reason.")
@app_commands.describe(
    member="The member to award points to",
    amount="Number of points to add",
    reason="Reason for awarding the points"
)
@app_commands.autocomplete(member=member_autocomplete)
async def addpoints(interaction: discord.Interaction, member: str, amount: float, reason: str):
    """
    Adds points to an existing member and records the change in the 'history' table.
    """
    cursor.execute(
        "SELECT points FROM points WHERE member_name = %s", (member,))
    row = cursor.fetchone()
    if not row:
        await interaction.response.send_message(
            f"Member '{member}' does not exist. Use /addmember first!",
            ephemeral=True
        )
        return

    old_points = row[0]
    new_points = old_points + amount

    cursor.execute(
        "UPDATE points SET points = %s WHERE member_name = %s",
        (new_points, member)
    )
    cursor.execute(
        "INSERT INTO history (member_name, reason, points) VALUES (%s, %s, %s)",
        (member, reason, amount)
    )

    await interaction.response.send_message(
        f"**{amount}** points have been added to **{member}** for: *{reason}*\n"
        f"New total: **{new_points}** points."
    )


@cabinet_only()
@bot.tree.command(name="removepoints", description="Remove points from an existing member, with a reason.")
@app_commands.describe(
    member="The member to remove points from",
    amount="Number of points to remove",
    reason="Reason for removing the points"
)
@app_commands.autocomplete(member=member_autocomplete)  # <--- attach here
async def removepoints(interaction: discord.Interaction, member: str, amount: float, reason: str):
    """
    Subtracts points from an existing member and records the change in the 'history' table.
    """
    cursor.execute(
        "SELECT points FROM points WHERE member_name = %s", (member,))
    row = cursor.fetchone()
    if not row:
        await interaction.response.send_message(
            f"Member '{member}' does not exist. Use /addmember first!",
            ephemeral=True
        )
        return

    old_points = row[0]
    new_points = old_points - amount

    cursor.execute(
        "UPDATE points SET points = %s WHERE member_name = %s",
        (new_points, member)
    )
    cursor.execute(
        "INSERT INTO history (member_name, reason, points) VALUES (%s, %s, %s)",
        (member, reason, -amount)
    )

    await interaction.response.send_message(
        f"**{amount}** points have been removed from **{member}** for: *{reason}*\n"
        f"New total: **{new_points}** points."
    )


@bot.tree.command(name="showpoints", description="Show a particular member's points or everyone's.")
@app_commands.describe(member="Optionally specify a member to show points for. If omitted, shows all.")
# <--- attach here, too (optional)
@app_commands.autocomplete(member=member_autocomplete)
async def showpoints(interaction: discord.Interaction, member: Optional[str] = None):
    """
    If 'member' is provided, show points for that one member.
    Otherwise, show points for everyone.
    """
    if member is None:
        # No member provided -> show all
        cursor.execute(
            "SELECT member_name, points FROM points ORDER BY member_name ASC")
        rows = cursor.fetchall()

        if not rows:
            await interaction.response.send_message("No members in the database yet!")
            return

        # Build table for all
        points_table = "```\nName         Points\n"
        points_table += "------------  ------\n"

        for (name_, pts) in rows:
            points_table += f"{name_:12}  {pts}\n"

        points_table += "```"
        await interaction.response.send_message(points_table)
    else:
        # Specific member -> show only that one
        cursor.execute(
            "SELECT member_name, points FROM points WHERE member_name = %s", (member,))
        row = cursor.fetchone()
        if not row:
            await interaction.response.send_message(
                f"Member '{member}' does not exist in the database."
            )
            return

        member_name, pts = row
        points_table = "```\nName         Points\n"
        points_table += "------------  ------\n"
        points_table += f"{member_name:12}  {pts}\n"
        points_table += "```"

        await interaction.response.send_message(points_table)


@bot.tree.command(name="showlogs", description="Show the historical logs for a given member.")
@app_commands.describe(member="The member whose history/logs you want to see.")
# <--- attach here as well
@app_commands.autocomplete(member=member_autocomplete)
async def showlogs(interaction: discord.Interaction, member: str):
    """
    Shows all history logs for a specific member, sorted by the creation time.
    """
    cursor.execute(
        "SELECT member_name FROM points WHERE member_name = %s", (member,))
    row = cursor.fetchone()
    if not row:
        await interaction.response.send_message(
            f"Member '{member}' does not exist. Use /addmember first!",
            ephemeral=True
        )
        return

    cursor.execute("""
        SELECT reason, points, created_at
        FROM history
        WHERE member_name = %s
        ORDER BY created_at ASC
    """, (member,))

    logs = cursor.fetchall()
    if not logs:
        await interaction.response.send_message(
            f"No history logs found for member '{member}'."
        )
        return

    # Build a table of logs
    logs_table = "```\nReason                           Points   Timestamp\n"
    logs_table += "--------------------------------  -------  ---------------------\n"

    for reason, points, ts in logs:
        reason_str = (reason[:30] + "...") if len(reason) > 30 else reason
        logs_table += f"{reason_str:32}  {points:7}  {ts}\n"

    logs_table += "```"
    await interaction.response.send_message(logs_table)


@bot.tree.command(name="showmembers", description="Show a list of all members in the database.")
async def showmembers(interaction: discord.Interaction):
    """
    Shows a list of all members in the database.
    """
    cursor.execute("SELECT member_name FROM points ORDER BY member_name ASC")
    rows = cursor.fetchall()

    if not rows:
        await interaction.response.send_message("No members in the database yet!")
        return

    members_list = "```\nMembers\n"
    members_list += "------------\n"
    for (name_,) in rows:
        members_list += f"{name_}\n"
    members_list += "```"

    await interaction.response.send_message(members_list)

import io
import openpyxl

@cabinet_only()
@bot.tree.command(name="addMembers_fromExcel", description="Upload an Excel file with First/Last Name columns to add members.")
@app_commands.describe(file="Excel file (.xlsx) with First Name and Last Name columns")
async def addmembers_fromexcel(interaction: discord.Interaction, file: discord.Attachment):
    # Check file type
    if not file.filename.endswith(".xlsx"):
        await interaction.response.send_message("Please upload a valid .xlsx Excel file.", ephemeral=True)
        return

    file_bytes = await file.read()
    workbook = openpyxl.load_workbook(io.BytesIO(file_bytes))
    sheet = workbook.active  # use first sheet

    headers = {cell.value.lower(): idx for idx, cell in enumerate(next(sheet.iter_rows(min_row=1, max_row=1)), start=1)}

    if "first name" not in headers or "last name" not in headers:
        await interaction.response.send_message("Excel must contain 'First Name' and 'Last Name' columns.", ephemeral=True)
        return

    first_idx = headers["first name"]
    last_idx = headers["last name"]

    added, skipped = [], []

    for row in sheet.iter_rows(min_row=2):
        first = row[first_idx-1].value
        last = row[last_idx-1].value
        if not first or not last:
            continue
        member_name = f"{first.strip()} {last.strip()}"

        # Check if exists
        cursor.execute("SELECT member_name FROM points WHERE member_name = %s", (member_name,))
        if cursor.fetchone() is None:
            cursor.execute("INSERT INTO points (member_name, points) VALUES (%s, %s)", (member_name, 0.0))
            added.append(member_name)
        else:
            skipped.append(member_name)

    # Build response
    msg = f"✅ Added {len(added)} members.\n"
    if skipped:
        msg += f"⚠️ Skipped {len(skipped)} existing members.\n"
    if added:
        msg += "Newly added: " + ", ".join(added[:10]) + ("..." if len(added) > 10 else "")

    await interaction.response.send_message(msg)


bot.run(DISCORD_TOKEN)
