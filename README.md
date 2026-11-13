# agent-plugins

u-ichi が公開する、エージェント向けの plugin の置き場所。どの plugin も、Claude Code の plugin と [Agent Plugins](https://agent-plugins.org/)（Codex、Cursor、GitHub Copilot など）の両方の形で置く。中身の skill は [Agent Skills](https://agentskills.io/) の `SKILL.md` で、どちらからも同じものを読む。

| plugin | 中身 |
|---|---|
| [business-figures](plugins/business-figures) | 事業企画、マーケティング、市場調査、事業計画などの資料や報告を、図を中心に作るための skill。論点の比べ方から図の形を選ぶ順、112種の図の見本（[figures.html](plugins/business-figures/skills/business-figures/references/figures.html)）、描き方を持つ |
| [dev-figures](plugins/dev-figures) | ソフトウェアの設計書、PR の説明、障害の報告などを、図を中心に作るための skill。見せたいことから図の形を選ぶ順、38種の図の見本（[figures.html](plugins/dev-figures/skills/dev-figures/references/figures.html)）、描き方を持つ |

## 入れ方

Claude Code：

```sh
/plugin marketplace add u-ichi/agent-plugins
/plugin install business-figures@u-ichi
/plugin install dev-figures@u-ichi
```

claude.ai（Claude のプロジェクトとクラウドのセッション）：https://claude.ai/customize/plugins の Add > Add marketplace で `u-ichi/agent-plugins` を足し、business-figures と dev-figures を追加してオンにする。

Agent Plugins に対応したツール：`plugins/business-figures/` と `plugins/dev-figures/` が plugin のフォルダ（直下に `plugin.json` と `skills/`）。入れ方はツールごとの説明に従う。

skill だけを使うとき：`plugins/<名前>/skills/<名前>/` を、使うツールの skill のフォルダに写す。

## 置き方

```
.claude-plugin/marketplace.json        Claude Code の marketplace
plugins/<名前>/plugin.json              Agent Plugins の manifest
plugins/<名前>/.claude-plugin/plugin.json  Claude Code の manifest
plugins/<名前>/skills/<skill>/SKILL.md   両方が読む skill
```

## ライセンス

MIT（[LICENSE](LICENSE)）。business-figures の図の分類と名前は Financial Times Visual Vocabulary（MIT）による。著作権の表示は [plugins/business-figures/LICENSES/FT-Visual-Vocabulary.txt](plugins/business-figures/LICENSES/FT-Visual-Vocabulary.txt)。
