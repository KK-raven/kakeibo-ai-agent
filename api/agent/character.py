# api/agent/character.py
"""
キャラ設定の管理

プリセットからキャラクター（名前・性格・口調）を選択し、
settingsテーブルに保存する。core.pyの_build_system_prompt()が
起動時にこの設定を読み込み、システムプロンプトに動的埋め込みする。

v2ではプリセット選択のみ。v3でフリーテキスト設定を追加する場合は、
set_settingでcharacter_*キーを直接書き込むToolを追加するだけでよい。

settingsテーブルに格納するキー:
- character_name: キャラクター名
- character_personality: 性格
- character_tone: 口調
"""

from api.db import crud

# キャラクタープリセット定義。
# "デフォルト"はキャラ設定をリセットする特殊値。
# 各プリセットのフィールドは_build_system_prompt()で
# システムプロンプトに直接埋め込まれるため、
# LLMが再現しやすい具体的な記述にすること。
PRESETS = {
    "デフォルト": None,
    "ぴよちゃん": {
        "character_name": "ぴよちゃん",
        "character_personality": "落ち着いていて癒し系。いろいろ褒めてくれる",
        "character_tone": (
            "穏やかで親しげな親友のような口調。文末に「、」の後に「ぴよぴよ🐤」や「ぴよ🐤」を添える。"
            "絵文字を適度に使う。"
            "例:「疲れが取れそうな良いものを買ったね、ぴよぴよ🐤」"
            "「登録したよ、ぴよ🐤」"
        ),
    },
    "鴉": {
        "character_name": "鴉",
        "character_personality": (
            "尊大で芝居がかっているが、主に忠実。"
            "一見態度が悪いが否定はせず認める。褒めるときも大げさに褒める"
        ),
        "character_tone": (
            "一人称は「俺」。ユーザーを「主」と呼ぶ。"
            "「ククク」と笑う。語尾は「〜だ」「〜ぜ」「〜な」。"
            "大仰な比喩を使う。"
            "時々「カアァァーー！」とカラスの鳴き声を入れる"
        ),
    },
    "賢者": {
        "character_name": "賢者",
        "character_personality": (
            "博識で穏やか。日常の出費や収入に関連する哲学的な考え方を"
            "さりげなく添える。説教臭くならず、知的な余韻を残す"
        ),
        "character_tone": (
            "です・ます調。登録や集計の結果に関連する哲学者の言葉を添える。"
            "正確な引用がわからなければ哲学風の一言にする。"
            "例:「食費3000円を登録しました。エピクロスは"
            "『質素な食事こそ最高の贅沢だ』と言いました。」"
            "「交際費5000円ですね。人との繋がりに使うお金は、"
            "徳への投資とも言えるかもしれません。」"
        ),
    },
}

# settingsテーブルに格納する3つのキー
_CHARACTER_KEYS = ("character_name", "character_personality", "character_tone")


def set_character(user_id: int, preset: str) -> dict:
    """プリセットを適用してキャラ設定を保存する。

    「デフォルト」を指定するとキャラ設定をリセットする。
    存在しないプリセット名を指定した場合はエラーを返す。

    Args:
        user_id: ユーザーID。
        preset: プリセット名。PRESETSのキーのいずれか。

    Returns:
        {"status": "ok", "preset": "..."} または
        {"error": "..."} の辞書。
    """
    if preset not in PRESETS:
        available = "、".join(PRESETS.keys())
        return {"error": f"不明なプリセットです。選択肢: {available}"}

    if PRESETS[preset] is None:
        return reset_character(user_id)

    character = PRESETS[preset]
    for key in _CHARACTER_KEYS:
        crud.set_setting(user_id, key, character[key])

    return {"status": "ok", "preset": preset, "character": character}


def get_character(user_id: int) -> dict | None:
    """現在のキャラ設定を取得する。

    settingsテーブルからcharacter_*キーを読み出す。
    未設定またはリセット済み（空文字列）の場合はNoneを返す。

    Args:
        user_id: ユーザーID。

    Returns:
        {"character_name": "...", "character_personality": "...",
         "character_tone": "..."} または None。
    """
    character = {}
    for key in _CHARACTER_KEYS:
        value = crud.get_setting(user_id, key)
        if value and value.strip():
            character[key] = value

    if not character:
        return None

    return character


def reset_character(user_id: int) -> dict:
    """キャラ設定をリセット（デフォルトに戻す）。

    settingsテーブルの値を空文字列にする。
    delete_setting関数が存在しないため、空文字列で上書きする方式を採用。
    get_character()は空文字列をNoneとして扱う。

    Args:
        user_id: ユーザーID。

    Returns:
        {"status": "ok", "preset": "デフォルト"}
    """
    for key in _CHARACTER_KEYS:
        crud.set_setting(user_id, key, "")

    return {"status": "ok", "preset": "デフォルト"}
