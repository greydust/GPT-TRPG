import discord
from discord import app_commands
from enum import Enum
from openai import OpenAI
import time
import json
import os
import atexit
import configparser
import threading
import traceback


config = configparser.ConfigParser()
config.read(os.path.join(os.path.dirname(__file__), '.config'))

DISCORD_TOKEN = config['DEFAULT']['DISCORD_TOKEN']
OPENAI_API_KEY = config['DEFAULT']['OPENAI_API_KEY']
SERVER_ID = int(config['DEFAULT']['SERVER_ID'])
CHANNEL_ID = int(config['DEFAULT']['CHANNEL_ID'])

rule_set = configparser.ConfigParser()
rule_set.read(os.path.join(os.path.dirname(__file__), 'rule_set.config'))
RULE_SET = {
  "main": {
    "file_name": rule_set['MAIN']['FILE_NAME'],
    "version": rule_set['MAIN']['VERSION'],
  }, 
  "character_creation": {
    "file_name": rule_set['CHARACTER_CREATION']['FILE_NAME'],
    "version": rule_set['CHARACTER_CREATION']['VERSION'],
  },
  "ability_check": {
    "file_name": rule_set['ABILITY_CHECK']['FILE_NAME'],
    "version": rule_set['ABILITY_CHECK']['VERSION'],
  },
  "combat": {
    "file_name": rule_set['COMBAT']['FILE_NAME'],
    "version": rule_set['COMBAT']['VERSION'],
  }
}

SAVES_FILE = "saves.json"

CHARACTER_CREATION_INTRO = [
  "從霧氣瀰漫的狹海彼岸，命運的長路悄然展開。",
  "七國仍在沉睡，而腳步已踏上未知的征途。",
  "長城之外冰雪靜默，南方的風正引我啟程。",
  "鐵與火尚未熄滅，群星之下，一條路正等待開闢。",
  "從古老學城的鐘聲到風息堡的遠影，我的旅程，悄然啟動。",
]

class CharacterCreationState(Enum):
  NOT_STARTED = 0
  CHARACTER_CREATION = 1
  JOINED = 2

class SessionState(Enum):
  NOT_STARTED = 0
  STARTED = 1
  ENDED = 2

# Custom JSON encoder and decoder for Enums
class EnumEncoder(json.JSONEncoder):
  def default(self, obj):
    if isinstance(obj, Enum):
      return obj.name  # Serialize Enum as its name
    return super().default(obj)

def enum_decoder(dct):
  for key, value in dct.items():
    if key == "state" and value in CharacterCreationState.__members__:
      dct[key] = CharacterCreationState[value]
    elif key == "state" and value in SessionState.__members__:
      dct[key] = SessionState[value]
  return dct

def load_saves():
  if os.path.exists(SAVES_FILE):
    try:
      with open(SAVES_FILE, "r", encoding="utf-8") as f:
        return json.load(f, object_hook=enum_decoder)
    except Exception as e:
      print(f"[Error] Failed to load saves: {e}")
  return {}

def save_saves():
  try:
    json_data = json.dumps(client.saves, ensure_ascii=False, indent=2, cls=EnumEncoder)
    with open(SAVES_FILE, "w", encoding="utf-8") as f:
      f.write(json_data)
  except Exception as e:
    print(f"[Error] Failed to save saves: {e}")

# Register save_saves to run at exit
atexit.register(save_saves)


class GPTTRPG(discord.Client):
  def __init__(self):
    super().__init__(intents=discord.Intents.default())
    self.tree = app_commands.CommandTree(self)
    self.saves = load_saves()
    self.playing = {}
    self.openAIClient = OpenAI(api_key=OPENAI_API_KEY)
    self.message_queue = {session_name: [] for session_name in self.saves.keys()}
    self.processing = {session_name: False for session_name in self.saves.keys()}
    self.processing_lock = {session_name: threading.Lock() for session_name in self.saves.keys()}
    self.rule_set = RULE_SET

  async def setup_hook(self):
    print("[Debug] Running setup_hook...")
    try:
      self.tree.copy_global_to(guild=discord.Object(id=SERVER_ID))
      await self.tree.sync(guild=discord.Object(id=SERVER_ID))
      print("[Debug] Slash commands synced.")
    except Exception as e:
      print(f"[Error] During setup_hook: {e}")
      traceback.print_exc()

    try:
      existing_assistants = self.openAIClient.beta.assistants.list().data
      existing_assistant_names = {assistant.name: assistant for assistant in existing_assistants}

      for key, rule in self.rule_set.items():
        assistant_name = f"GPTTRPG_{key}"
        metadata = {"version": rule["version"]}

        if assistant_name in existing_assistant_names:
          existing_metadata = existing_assistant_names[assistant_name].metadata
          if existing_metadata.get("version") == rule["version"]:
            print(f"[Debug] Assistant '{assistant_name}' already exists with the correct version.")
            self.rule_set[key]['assistant_id'] = existing_assistant_names[assistant_name].id
        
        if 'assistant_id' not in self.rule_set[key]:
          with open(rule["file_name"], "r", encoding="utf-8") as f:
            instructions = f.read()

          response = self.openAIClient.beta.assistants.create(
            name=assistant_name,
            model="gpt-4-turbo",
            metadata=metadata,
            instructions=instructions
          )
          self.rule_set[key]['assistant_id'] = response.id
          print(f"[Debug] Created assistant '{assistant_name}' with metadata {metadata}.")
    except Exception as e:
      print(f"[Error] During assistant setup: {e}")
      traceback.print_exc()

  async def create(self, interaction: discord.Interaction, session_name: str, scenario_id: str = None):
    if interaction.channel.id != CHANNEL_ID:
      await interaction.response.send_message("請在gpt-trpg頻道使用此指令.", ephemeral=True)
      return

    if session_name in self.saves:
      await interaction.response.send_message(f"❌ 進度 `{session_name}` 已存在.")
      return

    self.saves[session_name] = {
      'scenario_id': scenario_id,
      'summary': '',
      'players': {},
      'state': SessionState.NOT_STARTED,
    }
    save = self.saves[session_name]

    scenario_content = None
    if scenario_id:
      scenarios_dir = os.path.join(os.path.dirname(__file__), "scenarios")
      file_path = os.path.join(scenarios_dir, f"{scenario_id}.md")
      if not os.path.exists(file_path):
        del self.saves[session_name]
        await interaction.response.send_message(f"❌ 找不到劇本 `{scenario_id}`。")
        return
      with open(file_path, "r", encoding="utf-8") as f:
        scenario_content = f.read()

    await interaction.response.defer()

    try:
      main_assistant_id = self.rule_set['main']['assistant_id']
      thread = self.openAIClient.beta.threads.create()
      save['assistant_id'] = main_assistant_id
      save['thread_id'] = thread.id

      if scenario_content:
        system_message = f"System\n使用以下劇本開始遊戲：\n{scenario_content}"
      else:
        system_message = "System\n不使用劇本並開始遊戲"

      self.openAIClient.beta.threads.messages.create(
        thread_id=thread.id,
        role="user",
        content=system_message
      )
      run = self.openAIClient.beta.threads.runs.create(
          thread_id=thread.id,
          assistant_id=main_assistant_id,
      )
      while True:
        run_status = self.openAIClient.beta.threads.runs.retrieve(
            thread_id=thread.id,
            run_id=run.id
        )
        if run_status.status in ["completed", "failed", "cancelled", "expired"]:
          break
        time.sleep(1)

      if run_status.status != "completed":
          del self.saves[session_name]
          await interaction.followup.send(f"❌ 創建進度失敗：Bot錯誤。狀態： {run_status.status}")
          return

      messages = self.openAIClient.beta.threads.messages.list(thread_id=thread.id, limit=5)
      # Find the latest assistant message
      assistant_reply = None
      for msg in messages.data:
          if msg.role == "assistant":
              assistant_reply = msg.content[0].text.value if msg.content else ""
              break

      if not assistant_reply:
          del self.saves[session_name]
          await interaction.followup.send("❌ 創建進度失敗：沒有收到 AI 回覆。")
          return

      save['state'] = SessionState.STARTED
      await interaction.followup.send(f"✅ 已創建進度 `{session_name}`\n\n**遊戲敘事:**\n{assistant_reply}")
    except Exception as e:
      del self.saves[session_name]
      await interaction.followup.send(f"❌ 創建進度失敗：發生錯誤: {e}")
      traceback.print_exc()

  async def list_sessions(self, interaction: discord.Interaction):
    if interaction.channel.id != CHANNEL_ID:
      await interaction.response.send_message("請在gpt-trpg頻道使用此指令.", ephemeral=True)
      return

    if not self.saves:
      await interaction.response.send_message("目前沒有任何進度。")
      return
    msg = "目前進度列表：\n"
    for name in self.saves:
      msg += f"• {name}\n"
    await interaction.response.send_message(msg)

  async def summary(self, interaction: discord.Interaction, session_name: str):
    if interaction.channel.id != CHANNEL_ID:
      await interaction.response.send_message("請在gpt-trpg頻道使用此指令.", ephemeral=True)
      return

    if session_name not in self.saves:
      await interaction.response.send_message(f"❌ 進度 `{session_name}` 不存在.")
      return

    summary = self.saves[session_name]['summary']
    if not summary:
      await interaction.response.send_message(f"❌ 進度 `{session_name}` 尚無摘要。")
      return

    await interaction.response.send_message(f"**進度 `{session_name}` 的摘要：**\n\n{summary}")

  async def join(self, interaction: discord.Interaction, session_name: str, message: str = None):
    if interaction.channel.id != CHANNEL_ID:
      await interaction.response.send_message("請在gpt-trpg頻道使用此指令.", ephemeral=True)
      return

    if session_name not in self.saves:
      await interaction.response.send_message(f"❌ 進度 `{session_name}` 不存在.")
      return

    if self.saves[session_name]['state'] == SessionState.NOT_STARTED:
      await interaction.response.send_message(f"❌ 進度 `{session_name}` 創建中，請稍候。")
      return

    if self.saves[session_name]['state'] == SessionState.ENDED:
      await interaction.response.send_message(f"❌ 進度 `{session_name}` 已結束，無法加入。")
      return

    user_id = interaction.user.id
    user_name = interaction.user.name
    save = self.saves[session_name]
    if user_id in save['players']:
      if save['players'][user_id]['state'] == CharacterCreationState.NOT_STARTED:
        await interaction.response.send_message(f"創角中，請稍候。", ephemeral=True)
      elif save['players'][user_id]['state'] == CharacterCreationState.CHARACTER_CREATION or save['players'][user_id]['state'] == CharacterCreationState.JOINED:
        self.playing[user_id] = session_name
        await interaction.response.send_message(f"✅ 已切換至進度 `{session_name}` 。", ephemeral=True)
      return

    await interaction.response.defer()

    save['players'][user_id] = {
      "state": CharacterCreationState.NOT_STARTED,
      'character_creation': {}
    }
    character_save = save['players'][user_id]

    try:
      character_creation_assistant_id = self.rule_set['character_creation']['assistant_id']
      thread = self.openAIClient.beta.threads.create()
      character_save['character_creation']['assistant_id'] = character_creation_assistant_id
      character_save['character_creation']['thread_id'] = thread.id

      if not message:
        message = CHARACTER_CREATION_INTRO[hash(user_id) % len(CHARACTER_CREATION_INTRO)]
      self.openAIClient.beta.threads.messages.create(
        thread_id=thread.id,
        role="user",
        content=message
      )

      # Retrieve the response from the assistant
      run = self.openAIClient.beta.threads.runs.create(
          thread_id=thread.id,
          assistant_id=character_creation_assistant_id,
      )

      while True:
        run_status = self.openAIClient.beta.threads.runs.retrieve(
            thread_id=thread.id,
            run_id=run.id
        )
        if run_status.status in ["completed", "failed", "cancelled", "expired"]:
          break
        time.sleep(1)

      if run_status.status != "completed":
        del save['players'][user_id]
        await interaction.followup.send(f"❌ 加入進度失敗：Bot錯誤. 狀態: {run_status.status}")
        return

      messages = self.openAIClient.beta.threads.messages.list(thread_id=thread.id, limit=5)
      # Find the latest assistant message
      assistant_reply = None
      for msg in messages.data:
        if msg.role == "assistant":
          assistant_reply = msg.content[0].text.value if msg.content else ""
          break

      if not assistant_reply:
        del save['players'][user_id]
        await interaction.followup.send("❌ 加入進度失敗：沒有收到 AI 回覆。")
        return

      self.playing[user_id] = session_name
      character_save['state'] = CharacterCreationState.CHARACTER_CREATION
      await interaction.followup.send(f"「{message}」\n\n✅ `{user_name}` 已加入進度 `{session_name}`\n\n**遊戲敘事:**\n{assistant_reply}")
    except Exception as e:
      del save['players'][user_id]
      await interaction.followup.send(f"❌ 加入進度失敗：發生錯誤: {e}")
      traceback.print_exc()

  async def play(self, interaction: discord.Interaction, message: str):
    if interaction.channel.id != CHANNEL_ID:
      await interaction.response.send_message("請在gpt-trpg頻道使用此指令.", ephemeral=True)
      return

    user_id = interaction.user.id
    user_name = interaction.user.name
    if user_id not in self.playing:
      await interaction.response.send_message("❌ 你尚未加入任何遊玩進度. 請用 `/join` 加入進度.", ephemeral=True)
      return

    await interaction.response.defer()

    session_name = self.playing[user_id]
    session = self.saves[session_name]
    if session['players'][user_id]['state'] == CharacterCreationState.CHARACTER_CREATION:
      player_data = session['players'][user_id]
      thread_id = player_data['character_creation']['thread_id']
      assistant_id = player_data['character_creation']['assistant_id']

      self.openAIClient.beta.threads.messages.create(
        thread_id=thread_id,
        role="user",
        content=message
      )

      run = self.openAIClient.beta.threads.runs.create(
        thread_id=thread_id,
        assistant_id=assistant_id
      )

      while True:
        run_status = self.openAIClient.beta.threads.runs.retrieve(
          thread_id=thread_id,
          run_id=run.id
        )
        if run_status.status in ["completed", "failed", "cancelled", "expired"]:
          break
        time.sleep(1)

      if run_status.status != "completed":
        await interaction.response.send(f"❌ Bot錯誤. 狀態: {run_status.status}", ephemeral=True)
        return

      messages = self.openAIClient.beta.threads.messages.list(thread_id=thread_id, limit=5)
      assistant_reply = None
      for msg in messages.data:
        if msg.role == "assistant":
          assistant_reply = msg.content[0].text.value if msg.content else ""
          break

      if not assistant_reply:
        await interaction.response.send("❌ 沒有收到 AI 回覆。", ephemeral=True)
        return

      # if first line of assistant reply is "Character_Creation_Complete", send the character data to main
      if assistant_reply.startswith("Character_Creation_Complete\n"):
        character_data = assistant_reply[len("Character_Creation_Complete\n"):]
        session['players'][user_id]['state'] = CharacterCreationState.JOINED
        self.message_queue[session_name].append((interaction, f"System\n{user_id}加入遊戲，角色：\n{character_data}"))
      else:
        await interaction.response.send(f"**玩家{user_name}輸入:**\n{message}\n\n**遊戲敘事:**\n{assistant_reply}")
        return
    
    if session['players'][user_id]['state'] == CharacterCreationState.JOINED:
      self.message_queue[session_name].append((interaction, f"User ID:{user_id}\n{message}"))

    if not self.message_queue[session_name]:
      return

    with self.processing_lock[session_name]:
      if self.processing[session_name]:
        await interaction.response.defer()
        return
      self.processing[session_name] = True

    while self.message_queue[session_name]:
      current_interaction, current_message = self.message_queue[session_name].pop(0)
      thread_id = session['thread_id']
      assistant_id = session['assistant_id']

      user_name = current_interaction.user.name
      try:
        self.openAIClient.beta.threads.messages.create(
            thread_id=thread_id,
            role="user",
            content=f"User ID: {user_name}\n{current_message}",
        )

        run = self.openAIClient.beta.threads.runs.create(
            thread_id=thread_id,
            assistant_id=assistant_id,
        )

        while True:
          run_status = self.openAIClient.beta.threads.runs.retrieve(
              thread_id=thread_id,
              run_id=run.id
          )
          if run_status.status in ["completed", "failed", "cancelled", "expired"]:
            break
          time.sleep(1)

        if run_status.status != "completed":
          await current_interaction.followup.send(f"❌ Bot錯誤. 狀態: {run_status.status}")
          continue

        messages = self.openAIClient.beta.threads.messages.list(thread_id=thread_id, limit=5)
        # Find the latest assistant message
        assistant_reply = None
        for msg in messages.data:
          if msg.role == "assistant":
            assistant_reply = msg.content[0].text.value if msg.content else ""
            break

        if not assistant_reply:
          await current_interaction.followup.send("❌ 沒有收到 AI 回覆。")
          continue

        await current_interaction.followup.send(f"**玩家{user_name}輸入:**\n{current_message}\n\n**遊戲敘事:**\n{assistant_reply}")
      except Exception as e:
        await current_interaction.followup.send(f"❌ 發生錯誤: {e}")

    self.processing[session_name] = False


client = GPTTRPG()

@client.tree.command(name="create", description="創建新的遊玩進度")
@app_commands.describe(session_name="進度名稱", scenario_id="劇本ID(留空則使用自由劇本)")
async def create(interaction: discord.Interaction, session_name: str, scenario_id: str = None):
  await client.create(interaction, session_name, scenario_id)

@client.tree.command(name="list_sessions", description="顯示所有進度")
async def list_sessions(interaction: discord.Interaction):
  await client.list_sessions(interaction)

@client.tree.command(name="summary", description="觀看進度摘要")
@app_commands.describe(session_name="進度名稱")
async def summary(interaction: discord.Interaction, session_name: str):
  await client.summary(interaction, session_name)

@client.tree.command(name="join", description="加入遊玩進度，如果是第一次加入則會進入創角流程")
@app_commands.describe(session_name="進度名稱", message="創角開場白。僅第一次加入時有用。留空則會隨機產生")
async def join(interaction: discord.Interaction, session_name: str, message: str = None):
  await client.join(interaction, session_name, message)

@client.tree.command(name="play", description="與敘事 AI 互動")
@app_commands.describe(message="輸入你的角色行動或對話")
async def play(interaction: discord.Interaction, message: str):
  await client.play(interaction, message)

@client.tree.command(name="scenario_list", description="列出所有劇本")
async def scenario_list(interaction: discord.Interaction):
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

@client.tree.command(name="save", description="手動保存進度")
async def save(interaction: discord.Interaction):
  try:
    save_saves()
    await interaction.response.send_message("✅ 進度已成功保存。")
  except Exception as e:
    await interaction.response.send_message(f"❌ 保存進度失敗：{e}")

client.run(DISCORD_TOKEN)
