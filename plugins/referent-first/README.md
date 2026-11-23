# referent-first

名前（label）より先に、指す物（referent）を決めてから書くための plugin。どの返信でも常に守る rule と、対象文書で本文より先に対応表を作る skill の2つで成り立つ。指す物と役割を決めてからすでにある名前を探して使い、不要な造語、作業ラベル、短縮語、同じ物の言い換えを防ぐ。

## 基準

1. **名前より先に指す物を決める**：対象文書では7列の対応表を本文より先に独立保存してから本文を書き、短い物では書き始める前に指す物と使う名前を会話の中で先に決めてから書く。
2. **作業ラベルと1語の総称を置かない**：骨組み、見出し、表の列見出し、チェックリスト、結論に「観測をまとめる」のような作業ラベルや、「作業者」「相手」「動く所」「対象」のような1語の総称を置かない。
3. **新しい呼び名と短縮語を作らない**：名前のある物はそのまま繰り返し、同じ物を別の語に言い換えない。新しい名前が本当に要るときだけ対応表で指す物と役割を先に決めてから付け、初出で定義する。

## 適用範囲

`SKILL.md` の `description` には「返信、進捗チェックリスト、PR の説明、依頼文、設計資料、調査報告、対策案を書くとき」と「コードや画面の新しい名前を決めるとき」を記している。これらに該当する作業や、ユーザーが示した推論の順序を見出しやラベルにまとめるときにエージェントがこの skill を読み込む。ユーザー原文の引用、単純な機械編集、既存名だけを再利用する箇所、表現だけの推敲は対象外。

## 名前を探す先と造語が出やすい箇所

- **名前を探す先**：プロジェクト指示、skill、コード・設定・コマンド、製品の画面、製品やツールの説明の原文、用語集、ユーザーの発言、公的名称や定着した専門用語、一般的な日本語。
- **探す先に含めない文**：ほかのセッション、サブエージェント、ほかの AI が書いた文や、作業中に自分が書いたメモ・作業記録・結果の表にある呼び名は、既存名の根拠にしない。
- **同じ物の言い換えと比べる物の呼び名**：同じ物を返信や表をまたいで別の語に言い換えない。修正の前と後、2つの案、2つのブランチのように比べる物も「版」「新」「旧」「案A」と呼ばず、どのブランチのどのファイルか、何を変えた物かで書く。

## 短い物を書く手順

返信、進捗チェックリスト、PR の説明、issue、依頼文では、書き始める前に触れる指す物と使う名前を会話の中で先に決め、本文、題名、見出し、表、チェックリストのすべてで同じ名前を繰り返す。

## 対象文書で対応表を先に作る手順

設計資料、調査報告、対策案、新しい名前を決めるときは、本文より先に7列の対応表（出典、目的、具体対象、役割、前後関係、候補語、初出定義）で指す物と役割を決めてから候補語と初出定義を置き、別ファイルか別ターンで独立保存してから本文を書く。

## 環境ごとの読まれ方

この plugin は、常に読む rule と対象文書で読む skill（`skills/referent-first/SKILL.md`）の2つで成り立つ。環境ごとの読まれ方は次のとおり。

- **Antigravity**：`rules/AGENTS.md` を毎ターン常に読み込み、対象文書では `skills/referent-first/SKILL.md` を読む。
- **Claude Code**：plugin の `rules/` は読まないが、plugin を有効にしている間は `output-styles/referent-first.md`（`force-for-plugin: true`）が output style として毎回の要求に常に付く。output style ではなくルールファイルとして使いたい場合は、プロジェクトの `CLAUDE.md` や `.claude/rules/` に `rules/AGENTS.md` と同じ文を置く。
- **claude.ai のチャットと Cowork**：`rules/AGENTS.md` も `output-styles/referent-first.md` も読まず、`skills/referent-first/SKILL.md` だけを読む。

## 入れ方

Claude Code：

```sh
/plugin marketplace add u-ichi/agent-plugins
/plugin install referent-first@u-ichi
```

claude.ai、Agent Plugins に対応したツール、または skill 単体での導入手順はリポジトリの [README](../../README.md#入れ方) を参照。

## ファイル構成

```
plugin.json                                  Agent Plugins の manifest
.claude-plugin/plugin.json                   Claude Code の manifest
rules/AGENTS.md                              Antigravity が毎ターン常に読む rule（frontmatter なし）
output-styles/referent-first.md              Claude Code で plugin 有効時に常に付く output style（本文は rules/AGENTS.md と同じ）
skills/referent-first/SKILL.md               skill の本文（基準、適用範囲、名前を探す先、短い物と対象文書の手順）
```

## ライセンス

MIT。
