# 猎宝控制台 Design System

## 1. Atmosphere & Identity

一个安静但有力的运维指挥中心。暗色/亮色双模式，以猎宝绿为唯一 accent，营造森林中猎人营地的氛围：深沉、克制、一切尽在掌控。界面密度中等，信息层级通过微妙的明度/色相变化而非粗重的边框区分。

Signature：猎宝绿作为唯一交互色，深色模式下微微发光的绿色 toggle/按钮与炭灰面板形成克制而醒目的对比。

## 2. Color

### Palette

| Role | Token | Light | Dark | Usage |
|------|-------|-------|------|-------|
| Surface/primary | --bg | #f4f6f8 | #111417 | 页面背景 |
| Surface/panel | --panel | #ffffff | #191e24 | 卡片、面板 |
| Surface/panel-soft | --panel-soft | #f8fafc | #222832 | 可悬浮项背景 |
| Surface/card | --card-bg | #ffffff | #1d232b | 内容卡片 |
| Surface/control | --control-bg | #ffffff | #202732 | 控件背景 |
| Surface/field | --field-bg | #ffffff | #11161d | 输入框 |
| Text/primary | --text | #1f2933 | #e7ecf2 | 正文、标题 |
| Text/muted | --muted | #657386 | #99a6b8 | 辅助文字 |
| Border/default | --line | #d9dee7 | #303846 | 普通分隔线 |
| Border/strong | --line-strong | #c6ceda | #445064 | 输入框、强边框 |
| Border/hover | --hover-line | #9eabc0 | #64738b | 悬浮边框态 |
| Accent/hunter | --hunter | #3b8c5e | #5db882 | 主色：导航选中、primary 按钮、toggle 开启 |
| Accent/hunter-soft | --hunter-soft | #e6f4ee | #1c3d2d | 选中态底色 |
| Accent/hunter-line | --hunter-line | #a8d5b8 | #3d7a5a | 选中态边框 |
| Accent/hunter-hover | --hunter-hover | #2f7350 | #4e9e71 | 按钮 hover |
| Accent/hunter-glow | --hunter-glow | rgba(59,140,94,0.14) | rgba(93,184,130,0.18) | focus ring |
| Accent/blue | --accent | #2367c9 | #7db2ff | 蓝色辅助（部分旧版兼容） |
| Status/success | --success-text | #177245 | #8ee0ad | 成功文字 |
| Status/success-line | --success-line | #acd6bd | #427c5f | 成功边框 |
| Status/success-bg | --success-bg | #f1fbf4 | #173326 | 成功背景 |
| Status/danger | --danger | #b42318 | #ff9a91 | 危险文字 |
| Status/danger-soft | --danger-soft | #fff1f0 | #3b1d1f | 危险背景 |
| Status/danger-line | --danger-line | #e6b3ae | #744347 | 危险边框 |

### Rules
- **猎人绿**是唯一交互色。蓝色系（--accent）逐步淘汰，仅保留兼容。
- Status 色仅用于功能状态标记，不可作为装饰色。
- 表面层级通过背景色的明度/色相变化实现深度，尽量减少对阴影的依赖。

## 3. Typography

### Font Stack
```css
font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Microsoft YaHei", sans-serif;
```
中英文混排场景。中文使用微软雅黑，西文使用系统字体。

### Scale

| Level | Size | Weight | Usage |
|-------|------|--------|-------|
| Page title | 20px | 700 | 页面大标题 |
| Section title | 15px | 700 | 面板标题 |
| Card title | 14px | 700 | switch-card 标题 |
| Body | 14px | 400 | 默认正文 |
| Body/sm | 13px | 400 | 正文辅助、状态栏 |
| Caption | 12px | 400 | 辅助文字、meta 信息 |
| Label | 12px | 650 | field label、tab active |
| Overline | 11px | 600 | 极小标记（品牌副标题） |

### Rules
- 正文不低于 12px。
- 字体粗细使用 400(default)、650(semibold)、700(bold) 三级就够了。

## 4. Spacing & Layout

### Base Unit
所有间距基于 **4px**。

| Token | Value | Usage |
|-------|-------|-------|
| --space-1 | 4px | 极紧间距 |
| --space-2 | 8px | 按钮间 gap、紧凑列表 |
| --space-3 | 12px | side-panel 内边距、switch-grid gap |
| --space-4 | 16px | section 内边距、brand 区 padding |
| --space-5 | 20px | content 区 padding |
| --space-6 | 24px | 大间距 |
| --space-8 | 32px | 群模式间距 |

### Grid
- Shell: `260px sidebar | 1fr workspace | 380px right-panel`
- Breakpoints: 1200px (hide right-panel), 700px (single column)
- Border-radius 体系：`6px`（按钮/输入框/标签）、`8px`（面板/卡片）、`999px`（pill）

### Rules
- 不使用 magic number，全部映射到 token。

## 5. Components

### Button
- **Structure**: `<button class="button [variant]">`
- **Variants**: default, primary (hunter), ghost, danger, small
- **Dimensions**: height 34px (default), 30px (small), padding 0 13px (0 8px small)
- **Radius**: 6px
- **States**:
  - **default**: 1px solid var(--line-strong), bg var(--panel)
  - **hover**: border-color var(--hover-line), slight lift
  - **active/pressed**: scale(0.97), deeper bg
  - **focus**: box-shadow 0 0 0 3px var(--hunter-glow)
  - **disabled**: opacity 0.5, cursor not-allowed
- **Primary variant**: bg var(--hunter), color white, border var(--hunter)

### Toggle
- **Structure**: 42x24px pill, circular knob 18px
- **Off**: bg var(--toggle-off), knob white with shadow
- **On**: bg var(--hunter), knob slides 18px right
- **Transition**: 160ms ease

### Side Button
- **Structure**: full-width, min-height 44px, border-radius 6px
- **States**: default (transparent), hover (panel-soft), active (hunter-soft + hunter-line border)

### Switch Card
- **Structure**: flex row, min-height 58px, padding 12px, border 1px var(--line), radius 8px
- **Disabled**: opacity 0.58

### Tab
- **Structure**: height 32px, padding 0 12px, border 1px, radius 6px
- **Active**: hunter-soft bg, hunter border, hunter text, weight 650

### Panel
- **Structure**: bg var(--panel), border 1px var(--line), radius 8px, shadow var(--shadow)

### Field / Input
- **Structure**: height 34px, padding 0 10px, border 1px var(--line-strong), radius 6px
- **Focus**: border-color var(--hunter), box-shadow 0 0 0 3px var(--hunter-glow)
- **Textarea**: min-height 90px, padding 9px 10px

### Empty State
- **Structure**: centered, min-height 220px, dashed border var(--line-strong), radius 8px

### Status Pill
- **Structure**: inline-flex, min-height 28px, padding 4px 9px, border 1px, radius 6px

## 6. Motion & Interaction

| Type | Duration | Easing | Usage |
|------|----------|--------|-------|
| Micro | 100-150ms | ease-out | Toggle switch, button press |
| Standard | 180-250ms | ease | Hover transitions, panel fade |

### Rules
- 只对 `transform`、`opacity`、`background`、`border-color`、`box-shadow` 做 transition。
- 每个可交互元素必须有 hover + active + focus 状态。
- 尊重 `prefers-reduced-motion`。

## 7. Depth & Surface

### Strategy: borders-only + subtle shadow
面板之间以 `1px solid var(--line)` 区分层级。shadow 仅用于浅色模式下的 toggle knob 和 segmented control 选中态。

```
--shadow: 0 1px 2px rgba(16, 24, 40, 0.06);  /* light */
--shadow: 0 1px 2px rgba(0, 0, 0, 0.28);      /* dark */
```

按钮悬浮时产生轻微上浮效果（translateY(-1px) + shadow 加深），按下时下沉（scale(0.97)）。
