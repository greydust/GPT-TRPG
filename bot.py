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

    await interaction.response.defer()  # Show "thinking..."
    user_name = interaction.user.name
    try:
      openAIClient.beta.threads.messages.create(
         thread_id=current_thread_id,
         role="user",
         content=f"[User ID: {user_name}]\n{message}",
      )

      run = openAIClient.beta.threads.runs.create(
         thread_id=current_thread_id,
         assistant_id=ASSISTANT_ID
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

client.run(DISCORD_TOKEN)
