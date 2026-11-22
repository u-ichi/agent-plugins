# agent-plugins

u-ichi が公開する、AI エージェント向け plugin のリポジトリ。各 plugin は Claude Code の plugin 形式と [Agent Plugins](https://agent-plugins.org/)（Codex、Cursor、GitHub Copilot など）の両方に対応している。中身の skill は [Agent Skills](https://agentskills.io/) の `SKILL.md` であり、どちらの環境からも同じファイルを参照する。

| plugin | 概要 |
|---|---|
| [figure-docs](plugins/figure-docs) | 人に読ませる資料（報告、判断の資料、事業の資料、開発の説明）を図を中心に作成する skill。事業の図112種と開発の図38種の見本を備える。詳しくは [plugin の README](plugins/figure-docs/README.md) |
| [referent-first](plugins/referent-first) | 語より先に指す対象と名前を固定し、不要な造語、作業ラベル、短縮語、同じ物の言い換えを防ぐ skill。詳しくは [plugin の README](plugins/referent-first/README.md) |

## 入れ方

Claude Code：

```sh
/plugin marketplace add u-ichi/agent-plugins
/plugin install <plugin の名前>@u-ichi   # 例: figure-docs@u-ichi
```

claude.ai（Claude のプロジェクトとクラウドセッション）：https://claude.ai/customize/plugins の Add > Add marketplace で `u-ichi/agent-plugins` を追加し、使う plugin を選んで有効にする。

Agent Plugins に対応したツール：`plugins/<名前>/` が plugin のフォルダ（直下に `plugin.json` と `skills/` がある）。追加手順は各ツールの説明に従う。

skill だけを使うとき：`plugins/<名前>/skills/<skill>/` を、使うツールの skill 用フォルダにコピーする。

## ファイル構成

```
.claude-plugin/marketplace.json            Claude Code の marketplace
plugins/<名前>/plugin.json                 Agent Plugins の manifest
plugins/<名前>/.claude-plugin/plugin.json  Claude Code の manifest
plugins/<名前>/skills/<skill>/SKILL.md     両方が参照する skill
plugins/<名前>/README.md                   plugin の説明（仕組みと出力例）
plugins/<名前>/images/                     README で使う画像
```

## ライセンス

MIT（[LICENSE](LICENSE)）。plugin が利用する他者の著作物の表示は、各 plugin の `LICENSES/` にある。
