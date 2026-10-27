# 第三方资源与许可声明（THIRD-PARTY NOTICES）

本项目（MaiBot 群聊每日分析插件）是 AstrBot 插件
[astrbot_plugin_qq_group_daily_analysis](https://github.com/SXP-Simon/astrbot_plugin_qq_group_daily_analysis)
的 **MaiBot 移植重构版**。本项目自身以 **MIT** 许可发布（见 [LICENSE](LICENSE)）。

本文件逐项列出随本项目分发的**第三方内容**、其原始权利人与适用许可。
**本项目自身的 MIT 许可不覆盖下列第三方内容**；再分发这些内容时必须遵守各自的条款。

---

## 一、上游项目（代码与模板）

| 内容 | 位置 | 许可 | 权利人 |
|---|---|---|---|
| 分析维度设计、9 套主题模板（`atri` / `BlueArchive` / `HatsuneMiku` / `format` / `hack` / `retro_futurism` / `scrapbook` / `simple` / `spring_festival`）、人格标签映射、群相册上传思路 | `plugin.py`、`templates/` 下上述 9 个目录 | **MIT** | Copyright (c) 2025 Helian Nuits |
| `art_nouveau`（新艺术运动）主题模板 | `templates/art_nouveau/` | **MIT**（上游同仓） | Copyright (c) 2025 Helian Nuits |
| `maria`（圣母在上）主题模板 | `templates/maria/` | **MIT**（上游同仓） | Copyright (c) 2025 Helian Nuits |
| `deepseek_maid`（DeepSeek 鲸鱼女仆娘）**模板代码 / CSS / 饭碗图形** | `templates/deepseek_maid/*.html` 中的代码与样式部分 | **MIT**（上游同仓） | Copyright (c) 2025 Helian Nuits；模板设计与角色改绘：[StuGRua](https://github.com/StuGRua) |
| 云端文转图渲染服务 | 可选配置项指向的第三方服务（不随包分发） | 见其自身仓库 | [AstrBotDevs/astrbot-t2i-service](https://github.com/AstrBotDevs/astrbot-t2i-service) |

上游 MIT 许可全文要求保留版权声明与许可声明 —— 本项目已在 `LICENSE` 顶部、`README.md`
「八、致谢与许可」以及各主题模板的页脚署名中保留。

---

## 二、`deepseek_maid` 角色美术：CC BY-NC-SA 4.0（**重要，非 MIT**）

`deepseek_maid` 主题中的**角色形象**不是 MIT 内容，而是社区同人创作，适用
**[CC BY-NC-SA 4.0](https://creativecommons.org/licenses/by-nc-sa/4.0/)**（署名 — 非商业性使用 — 相同方式共享）。

**适用范围**：

- `templates/deepseek_maid/inline_assets.html` 中引用的角色 PNG 图层
- `assets/deepseek_maid/layers/basin-v3.png` 及同目录其余角色图层
  （`front.png`、`eye-0.png`、`eye-1.png`、`closed-eye-0.png`、`closed-eye-1.png`、`mouth.png`）
- `assets/previews/deepseek_maid.jpg`（预览图，含角色部分）
- 以及**生成报告中出现的角色部分**

**这意味着**：使用本套主题生成的报告图片，其角色部分是 CC BY-NC-SA 4.0 内容，
不得用于商业用途，且再分发时须以相同许可共享。若你需要完全无约束的产出，请改用其余 11 套主题。

原说明见 [`assets/deepseek_maid/SOURCE-NOTICE.md`](assets/deepseek_maid/SOURCE-NOTICE.md)。

### 署名链（CC BY-NC-SA 4.0 要求的 attribution）

- [上善无形（上善）](https://www.bilibili.com/opus/1231977657712771073) — 鲸鱼娘角色原设
- [ZipZipPipe](https://www.pixiv.net/users/18604994) — 融入 DeepSeek 元素的女仆版本设计
- [StuGRua](https://github.com/StuGRua) — 模板设计与角色改绘
- 参考素材来源链：
  [Small-tailqwq/dsh-deep-whale NOTICE](https://github.com/Small-tailqwq/dsh-deep-whale/blob/825d02b0e569c370ae15ceed12d9393559b3e4f4/maid-atelier/NOTICE)、
  [1190fasheqi/dafeiyu-pet](https://github.com/1190fasheqi/dafeiyu-pet/tree/5b0e01856116bd2bae82df1f43c32faa5f056196)
- 大铁盆造型参考：[Witherwithwinter/DeepSeek-Balance-Whale-Widget-Bowl](https://github.com/Witherwithwinter/DeepSeek-Balance-Whale-Widget-Bowl/tree/24feb651d7991cbefc46666422883635f06f333e)

---

## 三、`maria` 主题素材

`assets/maria/` 下的角色插画（`statue_virgin_mary.png`、`rosa_chinensis_sachiko_yumi.png`、
`rosa_foetida_yoshino_rei.png`、`rosa_gigantea_sei_shimako.png`、`yumi_and_sei.png`、
`sachiko_yumi_wind_ribbon.png`、`sei_window_observation.png`）与蔷薇徽章
（`rose_red.png` / `rose_white.png` / `rose_yellow.png`）由上游项目随其 MIT 许可的仓库分发，
上游未为其单列素材条款。

**请注意**：这些插画描绘的是《圣母在上》（マリア様がみてる）的角色，
**角色 IP 权利属于原作者/出版方**，MIT 许可不构成、也不能替代角色 IP 授权。
本项目与上游项目均为非商业的同人性质使用；若你打算将本主题用于商业用途，
请自行取得相应授权，或改用不含第三方角色 IP 的主题。

`assets/maria/persona.md` 是该主题专用的分析人格提示词，随上游 MIT 许可分发。

---

## 四、其余主题中的第三方角色

同为**非商业同人性质**、角色 IP 归原权利人的还包括：

| 主题 | 角色 |
|---|---|
| `atri`（ATRI） | 亚托莉 —《ATRI -My Dear Moments-》 |
| `bluearchive`（BlueArchive） | 阿洛娜 —《蔚蓝档案》 |
| `hatsunemiku`（初音未来） | 初音未来 — Crypton Future Media |
| `maria`（圣母在上） | 莉莉安女子学园角色 —《圣母在上》 |

上游与本项目均未就这些角色主张任何权利；如用于商业场景请自行取得授权。

---

## 五、字体与表情

| 内容 | 位置 | 许可 |
|---|---|---|
| 霞鹜文楷 LXGW WenKai（常用字子集） | `fonts/LXGW-Regular-sub.woff2` | [SIL Open Font License 1.1](https://scripts.sil.org/OFL) |
| Noto Color Emoji（分段内嵌 woff2） | `emoji_fonts.json` | [SIL Open Font License 1.1](https://scripts.sil.org/OFL) |
| 运行时从镜像加载的西文字体（Cinzel / Cormorant Garamond / Noto Serif SC 等） | 在线引用，不随包分发 | 各自 OFL / Apache-2.0 |
| 人格水印图资源索引 | `profile_assets.json` | 指向 [SXP-Simon/profile_assets](https://github.com/SXP-Simon/profile_assets) 公开图床 |

---

## 六、`assets/previews/` 下的主题预览图

`assets/previews/*.jpg` 是本项目用**自身渲染管线**、以自身模板对固定样例数据实时渲染生成，
不是从上游仓库复制的图片，本项目对其享有权利（其中 `deepseek_maid.jpg` 含角色部分，
仍受上文 CC BY-NC-SA 4.0 约束；`maria.jpg`、`atri.jpg`、`bluearchive.jpg`、`hatsunemiku.jpg`
含上文所述第三方角色形象）。

---

*本文件的权威版本随仓库更新；如与上游 `SOURCE-NOTICE` 等文件存在差异，以上游原始声明为准。*
*最后更新：2026-10-27*
