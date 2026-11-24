# claude-compact-restore

Claude Code の compact の後に、claude.ai のプロジェクト（Claude Projects）の「プロジェクト指示」の全文と、Claude Code が付け直さなかった skill の本文を補う plugin である。この plugin は hook だけで成り立ち、skill は持たない。hook に対応するのは Claude Code である。

プロジェクト指示は claude.ai のプロジェクト（Claude Projects）だけにある物である。プロジェクト指示の入れ直しは、claude.ai のプロジェクトから動く Claude Code のセッション向けの処理であり、プロジェクトを使わない Claude Code では設定しなくてよい（`CLAUDE.md` は Claude Code が compact の後に入れ直すため）。skill の呼び直しは、どの Claude Code でも使える。

Python 3.9 以上で動作し、標準ライブラリだけを使う。hook は失敗しても会話を止めず、設定や transcript が読めないときは何もしない。

## 補うもの

この plugin が補うのは次の2つだけである。

1. compact の直後に、プロジェクト指示を書いたファイルを読み込み、プロジェクト指示の全文を入れ直す（claude.ai のプロジェクトから動く Claude Code のセッション向け）。
2. compact の後に Claude Code が付け直さなかった skill について、Skill ツールでの呼び直しを求める。呼び直すまで、Skill と ToolSearch 以外の tool 呼び出しを止める。

### プロジェクト指示の入れ直し

この処理が入れ直すのは、claude.ai のプロジェクト（Claude Projects）の「プロジェクト指示」である。プロジェクト指示は会話の最初のメッセージにしか入らず、compact の要約で言い換えられて短くなるため、書き方などのルールが落ちる。これは不具合ではなく要約の性質によるものである。

Claude Code は `CLAUDE.md` を compact の後に自分で入れ直す。そのため `CLAUDE.md` はこの plugin の対象ではなく、プロジェクトを使わない Claude Code ではこの処理を設定しなくてよい。

Claude Code はプロジェクト指示をファイルとして持たないため、利用者はプロジェクト指示と同じ中身を書いたファイルを用意し、そのパスを設定する。Claude Code の compact では PreCompact、要約の作成、SessionStart（`source` が `compact`）、PostCompact の順に処理が進む。この plugin は SessionStart（`compact`）の hook でプロジェクト指示を書いたファイルを読み込み、前置きを付けてプロジェクト指示の全文を追加の文脈として出力する。

Claude Code は hook 1つの追加の文脈を UTF-16 の単位で 10,000 文字で切る。そのため、9,000 単位を超えるときは2つの hook に分けて出力する。およそ 18,000 単位を超え、2つでも入らない長さのときは、プロジェクト指示を書いたファイルを Read で読み直すよう求める文だけを出す。

### 付け直されなかった skill の呼び直し

後述する Claude Code の不具合により、プロセスの再開をはさんだ compact で skill の本文が付け直されないことがある。

この plugin では PreToolUse の hook が transcript を読み込み、最後の compact の境目より前に呼んだ skill のうち、次の条件をすべて満たすものについて Skill ツールでの呼び直しを求める。

- 設定のパターンに合う
- 境目の後の `invoked_skills` に無い
- 境目の後に呼び直されてもいない

呼び直すまでは Skill と ToolSearch 以外の tool 呼び出しを止める。すでに `invoked_skills` にある skill は求めないため、本文が二重に入ることはない。サブエージェントの呼び出しは対象外である。

### 補わないもの

次の4つは補わない。

- 利用者の発言の再掲
- 読んだファイルの読み直し
- 要約を作るモデルへの指示
- 進捗の一覧の再掲

合成の場面での測定（169 回）では、これらを補っても compact の後のルール違反の数に差が出なかった。プロジェクト指示の全文の入れ直しは、同じ測定でルール違反が平均 13.7 件から 1.5 件に減った。

## Claude Code の compact の不具合

Claude Code 2.1.328、2.1.330、2.1.331 で確認した不具合である。

Claude Code の compact は、プロセスが持つ「呼んだ skill の一覧」にある skill の本文を compact の後に付け直す。プロセスを終了して同じ会話を再開（resume）すると、この一覧は直前の compact で付け直した内容（それまで compact が無ければ空）に戻る。そのため、再開より前に呼んだ skill が一覧から落ち、次の compact で付け直されない。

![プロセスの再開と compact で skill の本文と Claude Code が持つ一覧が変わる流れ](images/skill-reattach.png)

図は横が時間の順（skill を呼ぶ、プロセスの終了、再開、compact）、縦が「会話の中の skill の本文」「Claude Code が持つ呼んだ skill の一覧」「モデルから skill が見えるか」の3段である。

Claude Code の中のどの処理を直すべきかは外からは分からない。再開時に一覧を会話の記録から作り直すのか、compact の処理が会話の記録を見るのかは判断できない。

### 起きる条件と起きやすい環境

この不具合は次の順に起きたときに発生する。

1. Skill ツールで skill を呼ぶ。
2. プロセスが終了して同じ会話を再開する。
3. compact が起きる。

再開だけでは skill の本文が会話に残るため害は出ない。同じプロセスの中で compact だけを行う場合も、呼んだ skill は全部付け直される。

この不具合は Claude Code のものであり、プロセスの終了と再開が多い使い方ほど起きやすい。claude.ai のプロジェクト（Claude Projects）では、返信を終えて次の入力を待つ間に Claude Code のプロセスが終了し、次の入力で会話を再開する。観測した会話では、返信を終えてから次の入力まで6分半以上空いた6回の全部で、プロセスが終了して再開していた。`claude -p --resume` を繰り返す使い方や、Claude Code を閉じて `--resume` で続ける使い方でも同じ順になる。

### 観測結果

3つの会話（Claude Code 2.1.328、2.1.330、2.1.331）で同じ欠落を確認した。

1つ目の会話では compact が2回起きた。

- 1回目（2026-11-24）は compact の前に4つの skill を呼んだ。うち2つはプロセスの再開より前、2つは再開の後に呼んだ。付け直されたのは再開の後に呼んだ2つだけだった。
- 2回目（同日）は1回目の後に呼び直しを含め5つの skill を呼んだが、その後にプロセスの再開が5回あった。付け直されたのは1回目に付け直された2つだけで、本文も1回目の時点のままだった。その間に skill が更新されていても古い本文が残った。

2つ目の会話でも compact が2回起きた。

- 1回目（2026-11-19、Claude Code 2.1.328）は compact の前に Skill ツールを6回呼び、その間にプロセスの再開が4回あった。compact の後に `invoked_skills` は付かず、skill は1つも付け直されなかった。
- 2回目（2026-11-22、Claude Code 2.1.330）は Skill ツールの呼び出しが累計17回、前の compact からのプロセスの再開が7回あった。付け直されたのは2つの skill だけだった。
- どちらの compact でも、Read ツールで読んだファイルは付け直された。この会話では作業の一覧（TaskCreate）を5件使っていたが、compact の前に全部完了していたため、未完了の作業の一覧が欠落するかは分からなかった。

3つ目の会話（Claude Code 2.1.330、2026-11-23 の compact）では、compact の前に Skill ツールを3回呼び、プロセスの再開が1回あり、compact の後に `invoked_skills` は付かなかった。この会話は未完了の作業（TaskCreate）が1件ある状態で compact したが、作業の一覧にあたる物は compact の後に付いていなかった。

同じプロセスの中で compact した測定では、呼んだ skill が 78 回中 78 回、全部付け直された。

### skill 以外の物

同じ観測（3つの会話、Claude Code 2.1.328、2.1.330、2.1.331）で、compact の直後に Claude Code が会話の記録に付け直した物を比べた。

| compact の後に付け直す物 | プロセスの再開より前の分 | 根拠 |
| --- | --- | --- |
| Skill ツールで呼んだ skill の本文（`invoked_skills`） | 欠落する | 1つ目の会話では2回ともプロセスの再開より前に呼んだ分が付かず、2つ目と3つ目の会話でも同じ欠落を確認した |
| Read ツールで読んだファイル（`compact_file_reference` と `file`） | 欠落しなかった | 1つ目の会話の2回目の compact で5回のプロセスの再開より前に読んだ3つのファイルが3つとも付き、2つ目の会話でも2回とも付け直された |
| ToolSearch で読み込んだ tool の一覧（`compact_boundary` の行の `compactMetadata.preCompactDiscoveredTools`） | 欠落しなかった | 1つ目の会話でプロセスの再開より前に読み込んだ tool が2回目の記録に残っていた |

作業の一覧（TaskCreate で作る物）、plan mode の計画、動いているバックグラウンドの処理の一覧は欠落するかどうか未確認である。作業の一覧については、2つ目の会話では5件とも compact の前に完了しており、3つ目の会話では未完了の作業が1件ある状態で compact したが、作業の一覧にあたる物は compact の後に付いていなかった。ただし Claude Code が作業の一覧を compact の後に付け直す作りかどうかが分からないため、欠落とは判断できない。plan mode の計画と動いているバックグラウンドの処理の一覧は、観測した会話では compact の時点で使っていなかった。

この plugin が補うのは skill だけである。未確認の物は補っていない。

### 確認の方法

Claude Code の更新後にこの不具合が残っているかは、会話の記録（transcript の JSONL）で確認できる。

1. `"subtype": "compact_boundary"` の行を探す。
2. その行より後にある `"type": "attachment"` で `attachment.type` が `invoked_skills` の行を開き、`skills[].name` を確認する。
3. 同じ `compact_boundary` より前にある assistant の `tool_use` のうち、`name` が `Skill` の行の `input.skill` と比べる。

呼んだのに一覧に無い skill が、付け直されなかった skill である。

## 設定

設定が無い項目の処理は動かない。環境変数が無い項目は設定ファイルから読む。

### 環境変数

| 環境変数 | 内容 |
| --- | --- |
| `CLAUDE_COMPACT_RESTORE_INSTRUCTIONS` | プロジェクト指示を書いたファイルのパス。`~` を展開する |
| `CLAUDE_COMPACT_RESTORE_SKILLS` | 呼び直しを求める skill の名前のパターン。fnmatch 形式のカンマ区切りで、`my-plugin:*,deploy` のように指定する |
| `CLAUDE_COMPACT_RESTORE_CONFIG` | 設定ファイルのパス。省略時は `~/.claude/claude-compact-restore.json` を使う |

### 設定ファイル

既定の設定ファイルは `~/.claude/claude-compact-restore.json` である。`instructions` に文字列、`skills` に文字列の配列を指定する。

```json
{
  "instructions": "~/project/INSTRUCTIONS.md",
  "skills": ["my-plugin:*"]
}
```

### skill パターンの指定

パターンは `plugin:skill` の形の名前と、`:` より後ろの名前のどちらかに合えばよい。

パターンを必ず指定させるのは、呼ぶと作業をやり直す skill（調査や定期実行を始める skill など）まで呼び直させないためである。手順やルールを読むための skill だけを指定する。

## 入れ方

Claude Code では次のコマンドを順に実行する。

```text
/plugin marketplace add u-ichi/agent-plugins
/plugin install claude-compact-restore@u-ichi
```

claude.ai では https://claude.ai/customize/plugins の **Add** > **Add marketplace** で `u-ichi/agent-plugins` を追加して有効にする。

## Claude Code 側で直ったら

プロセスの再開をはさんだ compact でも、呼んだ skill が全部 `invoked_skills` に入るようになれば、skill の呼び直しを求める処理は何も求めなくなる。この plugin は `invoked_skills` を見て判断するため、入れたままでも害は無い。

そのときは `skills`（環境変数を使っている場合は `CLAUDE_COMPACT_RESTORE_SKILLS`）の設定を消してよい。プロジェクト指示の入れ直しは不具合とは別なので残す。

## テスト

テストは次のコマンドで実行する。

```sh
python3 -m unittest discover -s plugins/claude-compact-restore/tests
```
