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
from collections import deque
import random
import io


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
    "rule_set": rule_set['MAIN']['RULE_SET'],
  }, 
  "character_creation": {
    "file_name": rule_set['CHARACTER_CREATION']['FILE_NAME'],
    "version": rule_set['CHARACTER_CREATION']['VERSION'],
    "rule_set": rule_set['CHARACTER_CREATION']['RULE_SET'],
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

SAVES_FILE = 'saves.json'
CHARACTER_FOLDER = 'characters'
SESSION_FOLDER = 'sessions'
SUMMARY_THRESHOLD_TOKEN = 20000

CHARACTER_CREATION_INTRO = [
  "從霧氣瀰漫的狹海彼岸，命運的長路悄然展開。",
  "七國仍在沉睡，而腳步已踏上未知的征途。",
  "長城之外冰雪靜默，南方的風正引我啟程。",
  "鐵與火尚未熄滅，群星之下，一條路正等待開闢。",
  "從古老學城的鐘聲到風息堡的遠影，我的旅程，悄然啟動。",
]

class PlayerState(Enum):
  NOT_STARTED = 0
  CHARACTER_CREATION = 1
  JOINED = 2

class CharacterCreationState(Enum):
  NOT_STARTED = 0
  CHARACTER_CREATION = 1
  CREATED = 2

class SessionState(Enum):
  NOT_STARTED = 0
  STARTED = 1
  ENDED = 2

class EnumEncoder(json.JSONEncoder):
  def default(self, obj):
    if isinstance(obj, Enum):
      return f"{obj.__class__.__name__}__{obj.name}"
    return super().default(obj)

def enum_decoder(dct):
  for key, value in dct.items():
    if isinstance(value, str) and "__" in value:
      enum_class_name, enum_member_name = value.split("__", 1)
      if enum_class_name in globals():
        enum_class = globals()[enum_class_name]
        if issubclass(enum_class, Enum) and enum_member_name in enum_class.__members__:
          dct[key] = enum_class[enum_member_name]
  return dct

def load_saves():
  save = {}
  characters = {}

  if os.path.exists(SAVES_FILE):
    try:
      with open(SAVES_FILE, "r", encoding="utf-8") as f:
        save = json.load(f, object_hook=enum_decoder)
    except Exception as e:
      print(f"[Error] Failed to load saves: {e}")

  if os.path.exists(CHARACTER_FOLDER):
    try:
      for user_id in os.listdir(CHARACTER_FOLDER):
        user_folder = os.path.join(CHARACTER_FOLDER, user_id)
        if os.path.isdir(user_folder):
          characters[user_id] = {}
          for character_file in os.listdir(user_folder):
            if character_file.endswith(".json"):
              character_path = os.path.join(user_folder, character_file)
              try:
                with open(character_path, "r", encoding="utf-8") as f:
                  character_id = os.path.splitext(character_file)[0]
                  characters[user_id][character_id] = json.load(f, object_hook=enum_decoder)
              except Exception as e:
                print(f"[Error] Failed to load character {character_file} for user {user_id}: {e}")
    except Exception as e:
      print(f"[Error] Failed to load characters: {e}")

  return save, characters

def save_saves():
  try:
    json_data = json.dumps(client.saves, ensure_ascii=False, indent=2, cls=EnumEncoder)
    with open(SAVES_FILE, "w", encoding="utf-8") as f:
      f.write(json_data)

    if not os.path.exists(CHARACTER_FOLDER):
      os.makedirs(CHARACTER_FOLDER)

    for user_id, characters in client.characters.items():
      user_folder = os.path.join(CHARACTER_FOLDER, user_id)
      if not os.path.exists(user_folder):
        os.makedirs(user_folder)

      for character_id, character_data in characters.items():
        character_file = os.path.join(user_folder, f"{character_id}.json")
        try:
          json_data = json.dumps(character_data, ensure_ascii=False, indent=2, cls=EnumEncoder)
          with open(character_file, "w", encoding="utf-8") as f:
            f.write(json_data)
        except Exception as e:
          print(f"[Error] Failed to save character {character_id} for user {user_id}: {e}")
  except Exception as e:
    print(f"[Error] Failed to save saves: {e}")

# Register save_saves to run at exit
atexit.register(save_saves)


class GPTTRPG(discord.Client):
  def __init__(self):
    super().__init__(intents=discord.Intents.default())
    self.openAIClient = OpenAI(api_key=OPENAI_API_KEY)
    self.tree = app_commands.CommandTree(self)
    self.saves, self.characters = load_saves()
    self.player_state = {}
    self.message_queue = {session_id: deque() for session_id in self.saves.keys()}
    self.processing = {session_id: False for session_id in self.saves.keys()}
    self.processing_lock = {session_id: threading.Lock() for session_id in self.saves.keys()}
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
            instructions=instructions,
            tools=[{"type": "file_search"}],
          )
          self.rule_set[key]['assistant_id'] = response.id
          print(f"[Debug] Created assistant '{assistant_name}' with metadata {metadata}.")
    except Exception as e:
      print(f"[Error] During assistant setup: {e}")
      traceback.print_exc()

  def sync_characters(self):
    for user_id, characters in self.characters.items():
      for character_id, character in characters.items():
        self.sync_character(user_id, character_id)

  def sync_character(self, user_id: str, character_id: str, refresh: bool = False) -> str:
    if user_id not in self.characters or character_id not in self.characters[user_id]:
      print(f"[Debug] No character data found for {user_id}: {character_id}")
      return

    character = self.characters[user_id][character_id]

    if character['state'] != CharacterCreationState.CREATED:
      print(f"[Debug] Character {character_id} for user {user_id} is created yet. State: {character['state']}")
      return
    
    if 'file_id' not in character or refresh:
      print(f"[Debug] Syncing character file for {user_id}: {character_id}")
      file_io = io.BytesIO(json.dumps(character['data'], ensure_ascii=False, indent=2).encode('utf-8'))
      file_io.name = f"CHARACTER_{user_id}_{character_id}.json"
      try:
        response = self.openAIClient.files.create(
          file=file_io,
          purpose="assistants",
        )
        character['file_id'] = response.id
      except Exception as e:
        print(f"[Error] Failed to upload character file for {user_id}: {character_id}: {e}")
        traceback.print_exc()
    
    return character['file_id']

  async def start_game(self, interaction: discord.Interaction, session_id: str, scenario_id: str = None):
    if interaction.channel.id != CHANNEL_ID:
      await interaction.response.send_message("請在gpt-trpg頻道使用此指令.", ephemeral=True)
      return

    if session_id in self.saves:
      await interaction.response.send_message(f"❌ 進度 `{session_id}` 已存在.")
      return

    self.saves[session_id] = {
      'scenario_id': scenario_id,
      'summaries': [],
      'players': {},
      'state': SessionState.NOT_STARTED,
      'rule_set': self.rule_set['main']['rule_set'],
    }
    save = self.saves[session_id]

    scenario_content = None
    if scenario_id:
      scenarios_dir = os.path.join(os.path.dirname(__file__), "scenarios")
      file_path = os.path.join(scenarios_dir, f"{scenario_id}.md")
      if not os.path.exists(file_path):
        del self.saves[session_id]
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
        content=system_message,
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
          del self.saves[session_id]
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
          del self.saves[session_id]
          await interaction.followup.send("❌ 創建進度失敗：沒有收到 AI 回覆。")
          return

      self.message_queue[session_id] = deque()
      self.processing[session_id] = False
      self.processing_lock[session_id] = threading.Lock()
      save['state'] = SessionState.STARTED
      await interaction.followup.send(f"✅ 已創建進度 `{session_id}`\n\n**遊戲敘事:**\n{assistant_reply}")
    except Exception as e:
      del self.saves[session_id]
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

  async def session_summary(self, interaction: discord.Interaction, session_id: str):
    if interaction.channel.id != CHANNEL_ID:
      await interaction.response.send_message("請在gpt-trpg頻道使用此指令.", ephemeral=True)
      return

    if session_id not in self.saves:
      await interaction.response.send_message(f"❌ 進度 `{session_id}` 不存在.", ephemeral=True)
      return

    summaries = self.saves[session_id]['summaries']
    if not summaries:
      await interaction.response.send_message(f"❌ 進度 `{session_id}` 尚無摘要。", ephemeral=True)
      return
    
    latest_summary = summaries[-1]
    file_path = os.path.join(SESSION_FOLDER, session_id, f"{latest_summary['file_name']}")
    if not os.path.exists(file_path):
      await interaction.response.send_message(f"❌ 找不到進度 `{session_id}` 的摘要檔案。", ephemeral=True)
      return
    
    with open(file_path, "r", encoding="utf-8") as f:
      await interaction.response.send_message(f"**進度 `{session_id}` 的最新摘要：**\n\n{f.read()}")

  async def create_character(self, interaction: discord.Interaction, character_id: str, message: str = None):
    if interaction.channel.id != CHANNEL_ID:
      await interaction.response.send_message("請在gpt-trpg頻道使用此指令.", ephemeral=True)
      return
    
    user_id = str(interaction.user.id)
    user_name = str(interaction.user.name)

    if user_id not in self.characters:
      self.characters[user_id] = {}
    
    if character_id in self.characters[user_id]:
      character = self.characters[user_id][character_id]
      if character['state'] == CharacterCreationState.CREATED:
        await interaction.response.send_message(f"❌ 角色 `{character_id}` 已存在，請使用其他ID。", ephemeral=True)
      elif character['state'] == CharacterCreationState.NOT_STARTED:
        await interaction.response.send_message(f"❌ 角色 `{character_id}` 創建中，請稍候。", ephemeral=True)
      elif character['state'] == CharacterCreationState.CHARACTER_CREATION:
        self.player_state[user_id] = {
          'state': PlayerState.CHARACTER_CREATION,
          'character_id': character_id,
        }
        await interaction.response.send_message(f"繼續創建角色 `{character_id}` 。")
      else:
        await interaction.response.send_message(f"❌ 角色 `{character_id}` 狀態未知：{character}。請重新創建。", ephemeral=True)
      return

    await interaction.response.defer()

    if character_id not in self.characters[user_id]:
      self.characters[user_id][character_id] = {
        'state': CharacterCreationState.NOT_STARTED,
      }
    character = self.characters[user_id][character_id]
    character_creation_assistant_id = self.rule_set['character_creation']['assistant_id']
    thread = self.openAIClient.beta.threads.create()
    character['assistant_id'] = character_creation_assistant_id
    character['thread_id'] = thread.id

    if not message:
      message = CHARACTER_CREATION_INTRO[random.randint(0, len(CHARACTER_CREATION_INTRO) - 1)]

    assistant_reply, error = self.run_and_fetch_thread_response(thread.id, character_creation_assistant_id, message)
    if error:
      del self.characters[user_id][character_id]
      await interaction.followup.send(error, ephemeral=True)
      return
    
    self.player_state[user_id] = {
      'state': PlayerState.CHARACTER_CREATION,
      'character_id': character_id,
    }
    character['state'] = CharacterCreationState.CHARACTER_CREATION,
    await interaction.followup.send(f"{user_name}：「{message}」\n\n**遊戲敘事:**\n{assistant_reply}")

  async def delete_character(self, interaction: discord.Interaction, character_id: str):
    if interaction.channel.id != CHANNEL_ID:
      await interaction.response.send_message("請在gpt-trpg頻道使用此指令.", ephemeral=True)
      return
    
    user_id = str(interaction.user.id)
    user_name = str(interaction.user.name)

    if user_id not in self.characters:
      await interaction.response.send_message("目前沒有任何角色。", ephemeral=True)
      return
    
    if character_id not in self.characters[user_id]:
      await interaction.response.send_message(f"❌ 角色 `{character_id}` 不存在。", ephemeral=True)
      return

    character_file = os.path.join(CHARACTER_FOLDER, user_id, f"{character_id}.json")
    if os.path.exists(character_file):
      try:
        os.remove(character_file)
      except Exception as e:
        await interaction.response.send_message(f"❌ 無法刪除角色檔案 `{character_id}`: {e}", ephemeral=True)
        return
    del self.characters[user_id][character_id]

    await interaction.response.send_message(f"✅ 角色 `{character_id}` 已刪除。", ephemeral=True)

  async def list_characters(self, interaction: discord.Interaction):
    if interaction.channel.id != CHANNEL_ID:
      await interaction.response.send_message("請在gpt-trpg頻道使用此指令.", ephemeral=True)
      return

    user_id = str(interaction.user.id)
    if user_id not in self.characters or not self.characters[user_id]:
      await interaction.response.send_message("目前沒有任何角色。", ephemeral=True)
      return
    
    msg = "目前角色列表：\n"
    for character_id, character in self.characters[user_id].items():
      name = ""
      if character['state'] == CharacterCreationState.NOT_STARTED:
        name = "初始化中"
      elif character['state'] == CharacterCreationState.CHARACTER_CREATION:
        name = "創角中"
      elif character['state'] == CharacterCreationState.CREATED:
        name = character['data']['name'] if 'data' in character and 'name' in character['data'] else "角色資料毀損"
      msg += f"• {character_id}: {name}\n"

    await interaction.response.send_message(msg, ephemeral=True)

  async def character_info(self, interaction: discord.Interaction, character_id: str):
    if interaction.channel.id != CHANNEL_ID:
      await interaction.response.send_message("請在gpt-trpg頻道使用此指令.", ephemeral=True)
      return

    user_id = str(interaction.user.id)
    if user_id not in self.characters or character_id not in self.characters[user_id]:
      await interaction.response.send_message(f"❌ 角色 `{character_id}` 不存在或未創建。", ephemeral=True)
      return
    
    character = self.characters[user_id][character_id]
    if character['state'] == CharacterCreationState.NOT_STARTED or character['state'] == CharacterCreationState.CHARACTER_CREATION:
      await interaction.response.send_message(f"❌ 角色 `{character_id}` 尚未創建完成。", ephemeral=True)
      return

    await interaction.response.send_message(f"**角色 `{character_id}` 的資訊：**\n\n{character['data']}", ephemeral=True)

  async def join(self, interaction: discord.Interaction, session_id: str, character_id: str = None, message: str = None):
    if interaction.channel.id != CHANNEL_ID:
      await interaction.response.send_message("請在gpt-trpg頻道使用此指令.", ephemeral=True)
      return

    if session_id not in self.saves:
      await interaction.response.send_message(f"❌ 進度 `{session_id}` 不存在.")
      return

    if self.saves[session_id]['state'] == SessionState.NOT_STARTED:
      await interaction.response.send_message(f"❌ 進度 `{session_id}` 創建中，請稍候。")
      return

    if self.saves[session_id]['state'] == SessionState.ENDED:
      await interaction.response.send_message(f"❌ 進度 `{session_id}` 已結束，無法加入。")
      return
    
    save = self.saves[session_id]
    user_id = str(interaction.user.id)
    user_name = str(interaction.user.name)

    if user_id in save['players']:
      self.player_state[user_id] = {
        'state': PlayerState.JOINED,
        'session_id': session_id,
      }
      await interaction.response.send_message(f"✅ 已切換至進度 `{session_id}` 。", ephemeral=True)
      return

    if character_id is None:
      await interaction.response.send_message("請提供角色ID。", ephemeral=True)
      return

    if user_id not in self.characters or character_id not in self.characters[user_id]:
      await interaction.response.send_message(f"❌ 角色 `{character_id}` 不存在，請先創建角色。", ephemeral=True)
      return
    
    if self.characters[user_id][character_id]['state'] != CharacterCreationState.CREATED:
      await interaction.response.send_message(f"❌ 角色 `{character_id}` 尚未創建完成，請先完成創建角色。。", ephemeral=True)
      return

    character_file_id = self.sync_character(user_id, character_id)
    character_data = self.characters[user_id][character_id]['data']
    save['players'][user_id] = {
      'character_id': character_id,
      'character_name': character_data['name'],
      'file_id': character_file_id,
    }
    main_assistant_id = save['assistant_id']
    thread_id = save['thread_id']

    await interaction.response.defer()

    if not message:
      message = CHARACTER_CREATION_INTRO[random.randint(0, len(CHARACTER_CREATION_INTRO) - 1)]

    self.message_queue[session_id].append({
        'interaction': interaction,
        'messages': [
          f"System\n{user_id}使用以下角色加入遊戲：\n{json.dumps(character_data, ensure_ascii=False, indent=2)}",
          f"User ID:{user_id}\n{message}",
        ],
        'response_prefix': f"**玩家{user_name}使用{character_data['name']}加入遊戲：{message}**",
        'attachments': [{'file_id': data, 'tools': [{'type': 'file_search'}]} for data in [character_file_id] if data is not None],
      }
    )
    await self.process_message_queue(session_id)
    self.player_state[user_id] = {
      'state': PlayerState.JOINED,
      'session_id': session_id,
    }

  async def play(self, interaction: discord.Interaction, message: str):
    if interaction.channel.id != CHANNEL_ID:
      await interaction.response.send_message("請在gpt-trpg頻道使用此指令.", ephemeral=True)
      return

    user_id = str(interaction.user.id)
    user_name = str(interaction.user.name)
    if user_id not in self.player_state or self.player_state[user_id]['state'] == PlayerState.NOT_STARTED:
      await interaction.response.send_message("❌ 尚未開始互動. 請用 `/join` 加入進度或使用 `/create` 創建角色.", ephemeral=True)
      return

    await interaction.response.defer()

    if self.player_state[user_id]['state'] == PlayerState.CHARACTER_CREATION:
      character_id = self.player_state[user_id]['character_id']
      assistant_id = self.characters[user_id][character_id]['assistant_id']
      thread_id = self.characters[user_id][character_id]['thread_id']

      assistant_reply, error = self.run_and_fetch_thread_response(thread_id, assistant_id, message)
      if error:
        await interaction.followup.send(error, ephemeral=True)
        return

      if "START_OF_CHARACTER" in assistant_reply and "END_OF_CHARACTER" in assistant_reply:
        message_index = assistant_reply.find("START_OF_CHARACTER")
        start_index = message_index + len("START_OF_CHARACTER")
        end_index = assistant_reply.find("END_OF_CHARACTER")

        assistant_message = assistant_reply[:message_index].strip()
        character_data_json = assistant_reply[start_index:end_index].strip()
        character = self.characters[user_id][character_id]
        try:
          character_data = json.loads(character_data_json)
          character['data'] = character_data
          character['state'] = CharacterCreationState.CREATED
          del character['assistant_id']
          del character['thread_id'] 
          self.player_state[user_id]['state'] = PlayerState.NOT_STARTED
          if assistant_message:
            assistant_message = f"**遊戲敘事**：{assistant_message}\n\n"
          await interaction.followup.send(f"{assistant_message}✅ 角色 `{character_id}` 創建完成！\n**角色資料：**\n{self.characters[user_id][character_id]['data']}")
        except json.JSONDecodeError as e:
          await interaction.followup.send(f"❌ 無法解析角色數據，創建角色失敗：{e}", ephemeral=True)
          traceback.print_exc()
        return
      
      await interaction.followup.send(f"**玩家{user_name}輸入:**\n{message}\n\n**遊戲敘事:**\n{assistant_reply}")
    
    elif self.player_state[user_id]['state'] == PlayerState.JOINED:
      session_id = self.player_state[user_id]['session_id']
      self.message_queue[session_id].append({
        'interaction': interaction,
        'messages': [
          f"User ID:{user_id}\n{message}",
        ],
        'response_prefix': f"**玩家{user_name}輸入:**\n{message}",
      })
      await self.process_message_queue(session_id)

  async def status(self, interaction: discord.Interaction):
    if interaction.channel.id != CHANNEL_ID:
      await interaction.response.send_message("請在gpt-trpg頻道使用此指令.", ephemeral=True)
      return
    
    user_id = str(interaction.user.id)
    if user_id not in self.player_state or self.player_state[user_id]['state'] == PlayerState.NOT_STARTED:
      await interaction.response.send_message("目前狀態：尚未開始互動", ephemeral=True)
      return
    
    if self.player_state[user_id]['state'] == PlayerState.CHARACTER_CREATION:
      character_id = self.player_state[user_id].get('character_id')
      await interaction.response.send_message(f"目前狀態：創建角色 `{character_id}` 中", ephemeral=True)
      return
    
    if self.player_state[user_id]['state'] == PlayerState.JOINED:
      session_id = self.player_state[user_id].get('session_id')
      await interaction.response.send_message(f"目前狀態：已加入進度 `{session_id}`", ephemeral=True)
      return
    
    await interaction.response.send_message("目前狀態：未知", ephemeral=True)

  def run_and_fetch_thread_response(self, thread_id: str, assistant_id: str, message: str, attachments: list = []) -> (str, str):
    try:
      self.openAIClient.beta.threads.messages.create(
        thread_id=thread_id,
        role="user",
        content=message,
        attachments=attachments
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
        traceback.print_exc()
        return None, f"❌ Bot錯誤. 狀態: {run_status.status}"

      messages = self.openAIClient.beta.threads.messages.list(thread_id=thread_id, limit=5)
      assistant_reply = None
      for msg in messages.data:
        if msg.role == "assistant":
          assistant_reply = msg.content[0].text.value if msg.content else ""
          break

      if not assistant_reply:
        traceback.print_exc()
        return None, f"❌ 沒有收到 AI 回覆。"

      return assistant_reply, None
    except Exception as e:
      traceback.print_exc()
      return None, f"❌ 發生錯誤: {e}"

  async def process_message_queue(self, session_id: str):
    if session_id not in self.message_queue or not self.message_queue[session_id]:
      return

    if session_id not in self.saves:
      print(f"[Error] Session {session_id} not found in saves.")
      return
    session = self.saves[session_id]

    with self.processing_lock[session_id]:
      if self.processing[session_id]:
        return
      self.processing[session_id] = True

    while self.message_queue[session_id]:
      payload = self.message_queue[session_id][0]
      current_interaction = payload['interaction']
      current_messages = payload['messages']
      current_response_prefix = payload.get('response_prefix', "")
      attachments = payload.get('attachments', [])

      thread_id = session['thread_id']
      assistant_id = session['assistant_id']

      try:
        for message in current_messages:
          self.openAIClient.beta.threads.messages.create(
              thread_id=thread_id,
              role="user",
              content=message,
              attachments=attachments
          )
      except Exception as e:
        await current_interaction.followup.send(f"❌ 發生錯誤: {e}")
      self.message_queue[session_id].popleft()

      try:
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

        await current_interaction.followup.send(f"{current_response_prefix}\n\n**遊戲敘事:**\n{assistant_reply}")

        print(f"[Debug] Total tokens used: {run_status.usage.total_tokens}\n")
        if run_status.usage.total_tokens > SUMMARY_THRESHOLD_TOKEN:
          self.summary_session(session_id)
      except Exception as e:
        await current_interaction.followup.send(f"❌ 發生錯誤: {e}")

    self.processing[session_id] = False

  def summary_session(self, session_id: str):
    if session_id not in self.saves:
      print(f"[Error] Session {session_id} not found in saves.")
      return
    
    session = self.saves[session_id]
    if session['state'] != SessionState.STARTED:
      print(f"[Error] Session {session_id} is not in a valid state for summarization.")
      return
    
    thread_id = session['thread_id']
    assistant_id = session['assistant_id']
    current_summary, error = self.run_and_fetch_thread_response(thread_id, assistant_id, "請以五百字內總結目前遊戲進度。回覆不需要使用命運引言，只需要完整敘述目前遊戲進度的摘要即可。")
    if error:
      print(f"[Error] Failed to summarize session {session_id}: {error}")
      return
    
    summary_name = f"Chapter {len(session['summaries']) + 1}"
    summary_file_name = f"{summary_name}.txt"
    with open(os.path.join(SESSION_FOLDER, session_id, summary_file_name), "w", encoding="utf-8") as f:
      f.write(current_summary)
    session['summaries'].append({
      'name': summary_name,
      'file_name': summary_file_name,
    })
    summary = session['summaries'][-1]

    file_io = io.BytesIO(current_summary.encode('utf-8'))
    file_io.name = f"SUMMARY_{session_id}_{summary_name}.txt"
    try:
      response = self.openAIClient.files.create(
        file=file_io,
        purpose="assistants",
      )
      summary['file_id'] = response.id
    except Exception as e:
      print(f"[Error] Failed to upload summary file for session {session_id}: {e}")
      traceback.print_exc()

    scenario_id = session['scenario_id']
    scenario_content = "無劇本"
    if scenario_id:
      scenarios_dir = os.path.join(os.path.dirname(__file__), "scenarios")
      file_path = os.path.join(scenarios_dir, f"{scenario_id}.md")
      if not os.path.exists(file_path):
        print(f"[Error] Scenario file for {scenario_id} not found.")
        return
      with open(file_path, "r", encoding="utf-8") as f:
        scenario_content = f.read()
    
    # get all file_ids from summaries and characters
    attachments = [
      {'file_id': file['file_id'], 'tools': [{'type': 'file_search'}]}
      for file in session['summaries']
      if 'file_id' in file
    ] + [
      {'file_id': player['file_id'], 'tools': [{'type': 'file_search'}]}
      for player in session['players'].values()
      if 'file_id' in player
    ]

    controlling_characters = ""
    for player_id, player in session['players'].items():
      controlling_characters += f"{player_id} 控制角色 {player['character_name']}\n"

    try:
      thread = self.openAIClient.beta.threads.create(
        messages=[
          {
            'content': f"System\n劇本：{scenario_content}\n\n目前摘要：{current_summary}\n\n{controlling_characters}",
            'role': 'user',
            'attachments': attachments,
          }
        ],
      )
      session['thread_id'] = thread.id
    except Exception as e:
      print(f"[Error] Failed to create summary thread for session {session_id}: {e}")
      traceback.print_exc()
      return

client = GPTTRPG()

@client.tree.command(name="start_game", description="創建新的遊玩進度")
@app_commands.describe(session_id="進度名稱", scenario_id="劇本ID(留空則使用自由劇本)")
async def start_game(interaction: discord.Interaction, session_id: str, scenario_id: str = None):
  await client.start_game(interaction, session_id, scenario_id)

@client.tree.command(name="list_sessions", description="顯示所有進度")
async def list_sessions(interaction: discord.Interaction):
  await client.list_sessions(interaction)

@client.tree.command(name="session_summary", description="觀看進度摘要")
@app_commands.describe(session_id="進度名稱")
async def session_summary(interaction: discord.Interaction, session_id: str):
  await client.session_summary(interaction, session_id)

@client.tree.command(name="create_character", description="創建角色")
@app_commands.describe(character_id="角色ID", message="創角開場白。留空則會隨機產生")
async def create_character(interaction: discord.Interaction, character_id: str, message: str = None):
  await client.create_character(interaction, character_id, message)

@client.tree.command(name="list_characters", description="列出所有角色")
async def list_characters(interaction: discord.Interaction):
  await client.list_characters(interaction)

@client.tree.command(name="delete_character", description="刪除角色")
@app_commands.describe(character_id="角色ID")
async def delete_character(interaction: discord.Interaction, character_id: str):
  await client.delete_character(interaction, character_id)

@client.tree.command(name="character_info", description="查看角色資訊")
@app_commands.describe(character_id="角色ID")
async def character_info(interaction: discord.Interaction, character_id: str):
  await client.character_info(interaction, character_id)

@client.tree.command(name="join", description="使用角色加入遊玩進度")
@app_commands.describe(session_id="進度名稱", character_id="角色ID，重新加入時可留空", message="加入開場白。留空則會隨機產生")
async def join(interaction: discord.Interaction, session_id: str, character_id: str = None, message: str = None):
  await client.join(interaction, session_id, character_id, message)

@client.tree.command(name="play", description="與敘事 AI 互動")
@app_commands.describe(message="輸入你的角色行動或對話")
async def play(interaction: discord.Interaction, message: str):
  await client.play(interaction, message)

@client.tree.command(name="status", description="查看玩家狀態")
async def status(interaction: discord.Interaction):
  await client.status(interaction)

@client.tree.command(name="list_scenarios", description="列出所有劇本")
async def list_scenarios(interaction: discord.Interaction):
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
random.seed(time.time())
