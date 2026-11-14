# agent-plugins

u-ichi が公開する、エージェント向けの plugin の置き場所。どの plugin も、Claude Code の plugin と [Agent Plugins](https://agent-plugins.org/)（Codex、Cursor、GitHub Copilot など）の両方の形で置く。中身の skill は [Agent Skills](https://agentskills.io/) の `SKILL.md` で、どちらからも同じものを読む。

| plugin | 中身 |
|---|---|
| [figure-docs](plugins/figure-docs) | 人に読ませる資料（報告、判断の資料、事業の資料、開発の説明）を、図を中心に作り切る skill。事業の図112種と開発の図38種の見本を持つ。詳しくは [plugin の README](plugins/figure-docs/README.md) |

## 入れ方

Claude Code：

```sh
/plugin marketplace add u-ichi/agent-plugins
/plugin install <plugin の名前>@u-ichi   # 例: figure-docs@u-ichi
```

claude.ai（Claude のプロジェクトとクラウドのセッション）：https://claude.ai/customize/plugins の Add > Add marketplace で `u-ichi/agent-plugins` を足し、使う plugin を追加してオンにする。

Agent Plugins に対応したツール：`plugins/<名前>/` が plugin のフォルダ（直下に `plugin.json` と `skills/`）。入れ方はツールごとの説明に従う。

skill だけを使うとき：`plugins/<名前>/skills/<skill>/` を、使うツールの skill のフォルダに写す。

## 置き方

```
.claude-plugin/marketplace.json        Claude Code の marketplace
plugins/<名前>/plugin.json              Agent Plugins の manifest
plugins/<名前>/.claude-plugin/plugin.json  Claude Code の manifest
plugins/<名前>/skills/<skill>/SKILL.md   両方が読む skill
plugins/<名前>/README.md               その plugin の説明（何をして、何が出るか）
plugins/<名前>/images/                 その README の画像
```

## ライセンス

MIT（[LICENSE](LICENSE)）。plugin が使う他者の著作物の表示は、各 plugin の `LICENSES/` にある。
