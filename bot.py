import discord
from discord import app_commands
from openai import OpenAI
import time
import json
import os
import atexit
import configparser


# ==== CONFIG ====
config = configparser.ConfigParser()
config.read(os.path.join(os.path.dirname(__file__), '.config'))

DISCORD_TOKEN = config['DEFAULT']['DISCORD_TOKEN']
OPENAI_API_KEY = config['DEFAULT']['OPENAI_API_KEY']
SERVER_ID = int(config['DEFAULT']['SERVER_ID'])
CHANNEL_ID = int(config['DEFAULT']['CHANNEL_ID'])
ASSISTANT_ID = config['DEFAULT']['ASSISTANT_ID']

SAVES_FILE = "saves.json"

def load_saves():
    if os.path.exists(SAVES_FILE):
        try:
            with open(SAVES_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"[Error] Failed to load saves: {e}")
    return {}

def save_saves():
    try:
        with open(SAVES_FILE, "w", encoding="utf-8") as f:
            json.dump(saves, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[Error] Failed to save saves: {e}")

saves = load_saves()  # save_name → thread_id
current_save = None
current_thread_id = None

# Register save_saves to run at exit
atexit.register(save_saves)

openAIClient = OpenAI(api_key=OPENAI_API_KEY)

class MyClient(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        print("[Debug] Running setup_hook...")
        try:
          self.tree.copy_global_to(guild=discord.Object(id=SERVER_ID))
          await self.tree.sync(guild=discord.Object(id=SERVER_ID))
          print("[Debug] Slash commands synced.")
        except Exception as e:
          print(f"[Error] During setup_hook: {e}")

client = MyClient()


@client.tree.command(name="create", description="創建新的存檔")
@app_commands.describe(save_name="存檔名稱")
async def create(interaction: discord.Interaction, save_name: str):
  if interaction.channel.id != CHANNEL_ID:
    await interaction.response.send_message("請在gpt-trpg頻道使用此指令.", ephemeral=True)
    return

  global saves, current_save, current_thread_id

  await interaction.response.defer()

  if save_name in saves:
      await interaction.followup.send(f"❌ 存檔 `{save_name}` 已存在.")
      return

  thread = openAIClient.beta.threads.create()
  current_thread_id = thread.id
  saves[save_name] = current_thread_id
  current_save = save_name

  await interaction.followup.send(f"✅ 已創建存檔 `{save_name}` 並切換到該存檔. Thread ID: `{current_thread_id}`")


@client.tree.command(name="load", description="讀取已存在的存檔")
@app_commands.describe(save_name="存檔名稱")
async def load(interaction: discord.Interaction, save_name: str):
  if interaction.channel.id != CHANNEL_ID:
    await interaction.response.send_message("請在gpt-trpg頻道使用此指令.", ephemeral=True)
    return

  global saves, current_save, current_thread_id

  await interaction.response.defer()

  try:
      if save_name not in saves:
          await interaction.followup.send(f"❌ 存檔 `{save_name}` 不存在.")
          return

      current_save = save_name
      current_thread_id = saves[save_name]
      await interaction.followup.send(f"✅ 已讀取存檔`{save_name}`. Thread ID: `{current_thread_id}`")
  except Exception as e:
      await interaction.followup.send(f"❌ 讀取存檔失敗: {e}")

@client.tree.command(name="play", description="與敘事 AI 互動")
@app_commands.describe(message="輸入你的角色行動或對話")
async def play(interaction: discord.Interaction, message: str):
  if interaction.channel.id != CHANNEL_ID:
    await interaction.response.send_message("請在gpt-trpg頻道使用此指令.", ephemeral=True)
    return

  global current_save, saves, current_thread_id
  if current_save not in saves:
    await interaction.response.send_message("❌ 無當前存檔. 請用 `/create` 創建新存檔或用 `/load` 讀取存檔.", ephemeral=True)
    return

  await interaction.response.defer()
  user_name = interaction.user.name
  try:
    openAIClient.beta.threads.messages.create(
        thread_id=current_thread_id,
        role="user",
        content=f"User ID: {user_name}\n{message}",
    )

    run = openAIClient.beta.threads.runs.create(
        thread_id=current_thread_id,
        assistant_id=ASSISTANT_ID,
    )

    # Poll for run completion
    while True:
      run_status = openAIClient.beta.threads.runs.retrieve(
          thread_id=current_thread_id,
          run_id=run.id
      )
      if run_status.status in ["completed", "failed", "cancelled", "expired"]:
        break
      time.sleep(1)

    if run_status.status != "completed":
        await interaction.followup.send(f"❌ Bot錯誤. 狀態: {run_status.status}")
        return

    messages = openAIClient.beta.threads.messages.list(thread_id=current_thread_id, limit=5)
    # Find the latest assistant message
    assistant_reply = None
    for msg in messages.data:
        if msg.role == "assistant":
            assistant_reply = msg.content[0].text.value if msg.content else ""
            break

    if not assistant_reply:
        await interaction.followup.send("❌ 沒有收到 AI 回覆。")
        return

    await interaction.followup.send(f"**玩家{user_name}輸入:**\n{message}\n\n**遊戲敘事:**\n{assistant_reply}")
  except Exception as e:
    await interaction.followup.send(f"❌ 發生錯誤: {e}")

@client.tree.command(name="list_saves", description="顯示所有存檔名稱")
async def list_saves(interaction: discord.Interaction):
  if interaction.channel.id != CHANNEL_ID:
    await interaction.response.send_message("請在gpt-trpg頻道使用此指令.", ephemeral=True)
    return

  global saves, current_save
  await interaction.response.defer()
  if not saves:
    await interaction.followup.send("目前沒有任何存檔。")
    return
  msg = "目前存檔列表：\n"
  for name in saves:
    if name == current_save:
      msg += f"• **{name}** (當前)\n"
    else:
      msg += f"• {name}\n"
  await interaction.followup.send(msg)

@client.tree.command(name="scenario_list", description="列出所有劇本")
async def scenario(interaction: discord.Interaction):
  if interaction.channel.id != CHANNEL_ID:
    await interaction.response.send_message("請在gpt-trpg頻道使用此指令.", ephemeral=True)
    return

  try:
      await interaction.response.defer()
      scenarios_dir = os.path.join(os.path.dirname(__file__), "scenarios")
      if not os.path.exists(scenarios_dir):
          await interaction.followup.send("❌ 找不到場景資料夾。")
          return

      scenario_files = [f for f in os.listdir(scenarios_dir) if f.endswith(".md")]
      if not scenario_files:
          await interaction.followup.send("❌ 沒有任何場景檔案。")
          return

      msg = "目前場景列表：\n"
      for file_name in scenario_files:
          file_id = file_name[:-3]  # Remove the .md extension
          file_path = os.path.join(scenarios_dir, file_name)
          with open(file_path, "r", encoding="utf-8") as f:
              first_line = f.readline().strip()
              title = first_line.lstrip("# ") if first_line.startswith("#") else first_line
              msg += f"• {file_id}: {title}\n"

      await interaction.followup.send(msg)
  except Exception as e:
      await interaction.followup.send(f"❌ 發生錯誤: {e}")

@client.tree.command(name="scenario_detail", description="顯示指定劇本的詳細資訊")
@app_commands.describe(scenario_id="劇本ID")
async def scenario_detail(interaction: discord.Interaction, scenario_id: str):
  if interaction.channel.id != CHANNEL_ID:
    await interaction.response.send_message("請在gpt-trpg頻道使用此指令.", ephemeral=True)
    return

  try:
      await interaction.response.defer()
      scenarios_dir = os.path.join(os.path.dirname(__file__), "scenarios")
      file_path = os.path.join(scenarios_dir, f"{scenario_id}.md")

      if not os.path.exists(file_path):
          await interaction.followup.send(f"❌ 找不到劇本 `{scenario_id}`。")
          return

      with open(file_path, "r", encoding="utf-8") as f:
          content = f.read()

      # Extract the first section starting with ##
      sections = content.split("##")
      if len(sections) < 2:
          await interaction.followup.send(f"❌ 劇本 `{scenario_id}` 沒有簡介。")
          return

      detail = sections[1].strip()
      await interaction.followup.send(f"**劇本 `{scenario_id}` 的簡介：**\n\n{detail}")
  except Exception as e:
      await interaction.followup.send(f"❌ 發生錯誤: {e}")

@client.tree.command(name="start_game", description="開始指定劇本的遊戲")
@app_commands.describe(scenario_id="劇本ID")
async def start_game(interaction: discord.Interaction, scenario_id: str):
  if interaction.channel.id != CHANNEL_ID:
    await interaction.response.send_message("請在gpt-trpg頻道使用此指令.", ephemeral=True)
    return

  await interaction.response.defer()

  global current_thread_id
  if not current_thread_id:
      await interaction.followup.send("❌ 沒有當前遊戲存檔，請先使用 `/create` 或 `/load` 指令。")
      return

  scenarios_dir = os.path.join(os.path.dirname(__file__), "scenarios")
  file_path = os.path.join(scenarios_dir, f"{scenario_id}.md")

  if not os.path.exists(file_path):
      await interaction.followup.send(f"❌ 找不到劇本 `{scenario_id}`。")
      return

  try:
    openAIClient.beta.threads.messages.create(
        thread_id=current_thread_id,
        role="user",
        content=f"請讀取{scenario_id}.md檔案並開始遊戲",
    )

    run = openAIClient.beta.threads.runs.create(
        thread_id=current_thread_id,
        assistant_id=ASSISTANT_ID,
    )

    # Poll for run completion
    while True:
      run_status = openAIClient.beta.threads.runs.retrieve(
          thread_id=current_thread_id,
          run_id=run.id
      )
      if run_status.status in ["completed", "failed", "cancelled", "expired"]:
        break
      time.sleep(1)

    if run_status.status != "completed":
        await interaction.followup.send(f"❌ Bot錯誤. 狀態: {run_status.status}")
        return

    messages = openAIClient.beta.threads.messages.list(thread_id=current_thread_id, limit=5)
    # Find the latest assistant message
    assistant_reply = None
    for msg in messages.data:
        if msg.role == "assistant":
            assistant_reply = msg.content[0].text.value if msg.content else ""
            break

    if not assistant_reply:
        await interaction.followup.send("❌ 沒有收到 AI 回覆。")
        return

    await interaction.followup.send(f"**遊戲敘事:**\n{assistant_reply}")
  except Exception as e:
    await interaction.followup.send(f"❌ 發生錯誤: {e}")

client.run(DISCORD_TOKEN)
