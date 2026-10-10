"""is_translatable() 回归测试 — 确保误提取不再发生。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from engines.base import TextItem
from utils.text_extract import (
    contains_kana,
    extract_placeholders,
    is_translatable,
    protect_placeholders,
    validation_source_for_item,
    verify_translation,
)


def test_kana_detection_excludes_katakana_middle_dot_bullet():
    assert not contains_kana("・侵蚀度达到100以上时迎来次日（SUNLIGHT路线）")
    assert contains_kana("SUNLIGHTルート")


def test_translation_with_middle_dot_bullet_is_not_rejected_as_kana_residual():
    original = "・「浸蝕度」が１００以上で翌日を迎える（SUNLIGHTルート）"
    translated = "・侵蚀度达到100以上时迎来次日（SUNLIGHT路线）"

    safe, warnings = verify_translation(original, translated)

    assert safe == translated
    assert not warnings


def test_speaker_angle_tag_kana_is_not_flagged_as_residual():
    """说话人标签 <ルルゥ> 在原文和译文中都必须保留，不应被误判为假名残留。"""
    original = "\\n<ルルゥ>「こんにちは、元気？」"
    translated = "\\n<ルルゥ>「你好，精神吗？」"

    safe, warnings = verify_translation(original, translated)

    assert safe == translated
    assert "假名残留" not in " ".join(warnings)


def test_speaker_tag_kana_residual_still_detected_in_dialogue_body():
    """标签保留但正文仍有假名时，仍应判为残留。"""
    original = "\\n<ルルゥ>「こんにちは、元気？」"
    translated = "\\n<ルルゥ>「こんにちは、你好」"

    safe, warnings = verify_translation(original, translated)

    assert safe == original  # 回退原文
    assert any("假名残留" in w for w in warnings)


def test_speaker_tag_removed_in_translation_is_not_residual():
    """Speaker tags are hard controls and must not be removed."""
    original = "\\n<ルルゥ>「こんにちは」"
    translated = "「你好」"

    safe, warnings = verify_translation(original, translated)

    assert safe == original
    assert warnings


def test_cjk_speaker_controls_are_protected_and_removed_from_visible_text():
    original = "{}<ルル>「きのこと野菜がある！」"
    translated = "{}<ルル>「有蘑菇和野菜！」"

    protected, placeholders = protect_placeholders(original)
    safe, warnings = verify_translation(original, translated)

    assert placeholders == ["{}", "<ルル>"]
    assert protected == "{{PH0}}{{PH1}}「きのこと野菜がある！」"
    assert extract_placeholders(original) == placeholders
    assert safe == translated
    assert not warnings


# ---- 应被过滤（返回 False）----

FALSE_POSITIVES = [
    # JS/TS 代码
    "let x = 42",
    "var name = 'test'",
    "const character = this.character(0);",
    "function foo() {",
    "return x + y;",
    "if (a > b) { return true; }",
    "for (let i = 0; i < 10; i++) {",
    "while (true) { break; }",
    "switch (mode) { case 1: break; }",
    "typeof obj === 'object'",
    "throw new Error('fail');",
    "try { something(); } catch(e) {}",
    "class MyClass extends BaseClass {",
    "import { foo } from 'bar';",
    "export default function() {}",
    "async function fetch() { await x; }",
    "require('module');",
    "process.exit(0);",
    "public static void main()",
    "private _hidden = null;",
    "undefined === null",

    # Windows 路径
    r"C:\Users\example\game-translator",
    r"C:\Program Files\Game\data",
    r"..\assets\textures",
    r".\config\settings",

    # 函数/方法调用
    "setLevel(5);",
    "myFunc(args)",
    "obj.method().another()",
    "character._interpreter = null;",
    "character._trigger = null;",
    "get_value(x, y)",

    # 赋值语句
    "x = 42",
    "name = 'test'",
    "result = calc(a, b)",

    # SQL
    "SELECT * FROM users",
    "INSERT INTO table VALUES (1)",
    "UPDATE items SET name = 'x'",
    "DELETE FROM logs WHERE id = 1",
    "CREATE TABLE test (id INT)",
    "DROP TABLE temp",

    # 全大写标识符（非 UI）
    "TEXTURE",
    "INLINE",
    "RANDOM",
    "STEP",
    "AUTOPLAY",
    "DISABLED",
    "EFFECT_SIZE",
    "MAX_VALUE",
    "HASH_ALGORITHM",

    # 单字符大写字母
    "A",
    "E",
    "X",
    "K",

    # 纯代码标识符
    "counter",
    "playerHealth",
    "EventHandler",
    "id_123",
    "pos2",
    "G1",
    "bg_club_day",
    "natsuki_ghost3",
    "_hidden",
    "sprite_index",

    # 场景/章节 ID
    "03_B",
    "03_B1",
    "02_A",
    "02_A_event",

    # 纯符号/标点
    "...",
    "}]",
    ", name, ",
    "(nil)",
    "{}",

    # 格式字符串
    "%s",
    "%02d",
    "%.2f",

    # Hash
    "a1b2c3d4e5f6a7b8",
    "DEADBEEFCAFE",

    # 纯数字
    "12345",
    "3.14159",
]


# ---- 应通过（返回 True）----

TRUE_POSITIVES = [
    # 英文对话
    "Hello, how are you?",
    "Save your progress?",
    "Click here to continue",
    "Are you sure you want to quit?",
    "Please select a character.",
    "Your inventory is full.",
    "The door is locked. You need a key.",

    # 日文对话
    "こんにちは、元気ですか？",
    "本当に終了しますか？",
    "セーブしますか？",
    "ここをクリックして続けてください",

    # 中文对话
    "你好，你还好吗？",
    "确定要退出吗？",
    "请选择一个角色",

    # 短 UI 文本
    "OK",
    "Yes",
    "No",
    "Save",
    "Load",
    "Quit",
    "Cancel",
    "ON",
    "OFF",

    # 较长的 UI 文本
    "New Game",
    "Continue",
    "Options",
    "Main Menu",
    "Load Game",
    "Start Game",

    # 带控制符的游戏文本
    "\\N[1]Hello, how are you?",
    "\\C[5]Please select a character.",
    "<b>Warning!</b> This action cannot be undone.",
    "{i}Important message{/i}",

    # 日文 + 控制符
    "\\N[1]こんにちは！",
    "\\C[2]本当に終了しますか？",
]


def test_false_positives():
    """所有误提取用例都应返回 False。"""
    failures = []
    for text in FALSE_POSITIVES:
        result = is_translatable(text)
        if result:
            failures.append(text)
    if failures:
        print(f"FAIL: {len(failures)} 条误提取文本未被过滤:")
        for t in failures:
            print(f"  - {t!r}")
    else:
        print(f"PASS: 所有 {len(FALSE_POSITIVES)} 条误提取用例已正确过滤")
    assert not failures, f"false positives should be filtered: {failures!r}"


def test_true_positives():
    """所有正确文本都应返回 True。"""
    failures = []
    for text in TRUE_POSITIVES:
        result = is_translatable(text)
        if not result:
            failures.append(text)
    if failures:
        print(f"FAIL: {len(failures)} 条正确文本被错误过滤:")
        for t in failures:
            print(f"  - {t!r}")
    else:
        print(f"PASS: 所有 {len(TRUE_POSITIVES)} 条正确文本未被误拦")
    assert not failures, f"true positives should remain translatable: {failures!r}"


def test_bracketed_dialogue_is_not_protected_as_placeholder():
    original = "@proto1 [よし、みんな揃ったね。]"
    translated = "@proto1 [好，大家都到齐了。]"

    protected, placeholders = protect_placeholders(original)

    assert placeholders == ["@proto1"]
    assert protected == "{{PH0}} [よし、みんな揃ったね。]"
    assert extract_placeholders(original) == ["@proto1"]
    assert verify_translation(original, translated)[0] == translated


def test_square_engine_tags_stay_protected():
    protected, placeholders = protect_placeholders("[gtext] hello [0]")

    assert protected == "{{PH0}} hello {{PH1}}"
    assert placeholders == ["[gtext]", "[0]"]


def test_kirikiri_percent_controls_stay_protected_as_whole_tokens():
    original = "A%p-1;%fＭＳ ゴシック;――%p;%fuser;B%50;"

    protected, placeholders = protect_placeholders(original)

    assert protected == "A{{PH0}}{{PH1}}――{{PH2}}{{PH3}}B{{PH4}}"
    assert placeholders == ["%p-1;", "%fＭＳ ゴシック;", "%p;", "%fuser;", "%50;"]
    assert extract_placeholders(original) == placeholders


def test_kirikiri_percent_controls_restore_and_validate():
    original = "それくらい間近に顔がある%p-1;%fＭＳ ゴシック;――%p;%fuser;わけではない"
    translated = "并不是因为脸靠得那么近%p-1;%fＭＳ ゴシック;――%p;%fuser;就兴奋起来"

    safe, warnings = verify_translation(original, translated)

    assert safe == translated
    assert not warnings


def test_kirikiri_percent_controls_reject_altered_font_control():
    original = "「ただ%p-1;%fＭＳ ゴシック;――%p;%fuser;」"
    translated = "「只是%p-1;%fMS Gothic;――%p;%fuser;」"

    safe, warnings = verify_translation(original, translated)

    assert safe == original
    assert warnings


def test_kirikiri_emphasis_markers_stay_protected():
    original = "「[・]君[・]は、[・]僕[・]に[・]勝[・]て[・]る[・]の[・]か？」"

    protected, placeholders = protect_placeholders(original)

    assert placeholders == ["[・]"] * 9
    assert protected == "「{{PH0}}君{{PH1}}は、{{PH2}}僕{{PH3}}に{{PH4}}勝{{PH5}}て{{PH6}}る{{PH7}}の{{PH8}}か？」"


def test_kirikiri_emphasis_marker_is_soft_missing():
    original = "並んで座っている子が、[・]僕を横目に見ている。"
    translated = "并排坐着的孩子斜眼瞥了我一下。"

    safe, warnings = verify_translation(original, translated)

    assert safe == translated
    assert warnings


def test_kirikiri_emphasis_middle_dot_variant_is_normalized():
    original = "並んで座っている子が、[・]僕を横目に見ている。"
    translated = "并排坐着的孩子，[·]斜眼瞥了我一眼。"

    safe, warnings = verify_translation(original, translated)

    assert safe == "并排坐着的孩子，[・]斜眼瞥了我一眼。"
    assert warnings


def test_bgi_ruby_validation_uses_visible_text():
    item = TextItem(
        file="data01110.arc::pg00_com",
        original="送信者の名前は、『<rギフトアップル>ＧｉｆｔＡｐｆｅｌ</r>』。",
        context="message",
        meta={"arc": "data01110.arc", "archive_kind": "arc20", "entry": "pg00_com"},
    )

    source = validation_source_for_item(item)
    safe, warnings = verify_translation(source, "发信人的名字是『ＧｉｆｔＡｐｆｅｌ』。")

    assert source == "送信者の名前は、『ＧｉｆｔＡｐｆｅｌ』。"
    assert safe == "发信人的名字是『ＧｉｆｔＡｐｆｅｌ』。"
    assert not warnings


def test_bgi_ruby_translation_source_drops_pronunciation_markup():
    from utils.text_extract import translation_source_for_item

    item = TextItem(
        file="data01110.arc::pg00_com",
        original="『<Rあき>安芸</R> かのこ』小学校のときからずっと同じクラスな上、家族ぐるみでも親交のある幼馴染。",
        context="message",
        meta={"arc": "data01110.arc", "archive_kind": "arc20", "entry": "pg00_com"},
    )

    source = translation_source_for_item(item)

    assert source == "『安芸 かのこ』小学校のときからずっと同じクラスな上、家族ぐるみでも親交のある幼馴染。"
    assert "<R" not in source
    assert "</R>" not in source


def test_non_bgi_angle_tags_remain_hard_placeholders():
    safe, warnings = verify_translation("<b>Warning!</b>", "警告！")

    assert safe == "<b>Warning!</b>"
    assert warnings


if __name__ == "__main__":
    print("=== is_translatable() 回归测试 ===\n")
    test_false_positives()
    print()
    test_true_positives()
    print()
    print("全部通过！")
