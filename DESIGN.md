# 猎宝控制台 — 设计系统

## 1. 氛围与识别

猎宝控制台是一个**深处的指挥所**。界面让人联想到猎人在密林营地的体验：周围的暗色如树影般包裹着你，猎人绿是穿梭在枝叶间的生机，金色是黄昏时分穿透树冠的日光，也是猎获的勋章。

**Signature（核心记忆点）**：侧边栏激活态左侧的猎人绿竖条 + 金色十字准星图标。这两种元素在暗色背景上形成强烈的识别信号——像森林里的一束信号弹。

## 2. 颜色

### 调色板

| 角色 | Token | 亮色 | 暗色 | 用途 |
|------|-------|------|------|------|
| 背景/最深 | --bg-deep | #f0f2f0 | #080c0e | body 背景，最深层级 |
| 表面/主要 | --surface-primary | #ffffff | #11181c | 侧边栏、主面板 |
| 表面/次要 | --surface-secondary | #f5f7f5 | #1a2328 | 卡片、面板 |
| 表面/升起 | --surface-elevated | #ffffff | #222d33 | 弹窗、下拉菜单 |
| 表面/玻璃 | --surface-glass | rgba(255,255,255,0.85) | rgba(26,35,40,0.85) | 毛玻璃卡片 |
| 文字/主要 | --text-primary | #1a241e | #e8edf0 | 标题、正文 |
| 文字/次要 | --text-secondary | #5a6b61 | #8896a4 | 说明、标注 |
| 文字/第三级 | --text-tertiary | #8a9b91 | #5a6b65 | 禁用、极弱 |
| 边框/默认 | --border-default | #d0d8d0 | #263238 | 分隔线、轮廓 |
| 边框/柔和 | --border-subtle | #e5ebe5 | #1c282e | 柔和分割 |
| 猎人绿/主色 | --hunter | #3b8c5e | #5db882 | 主色 —— 按钮、激活态、链接 |
| 猎人绿/悬停 | --hunter-hover | #2f7350 | #4e9e71 | 悬停态 |
| 猎人绿/柔和 | --hunter-soft | #e6f4ee | #1c3d2d | 选中状态背景 |
| 猎人绿/线条 | --hunter-line | #a8d5b8 | #3d7a5a | 选中边框 |
| 猎人绿/辉光 | --hunter-glow | rgba(59,140,94,0.14) | rgba(93,184,130,0.18) | focus ring |
| 金色/主色 | --gold | #c8a84e | #e0c66a | 强调色 —— 稀有标记、奖杯 |
| 金色/柔和 | --gold-soft | #faf5e8 | #2d2815 | 金色背景 |
| 金色/线条 | --gold-line | #e0d4a8 | #5a4d28 | 金色边框 |
| 状态/成功 | --status-success | #16a34a | #22c55e | 确认 |
| 状态/警告 | --status-warning | #d97706 | #f59e0b | 警告 |
| 状态/错误 | --status-error | #dc2626 | #ef4444 | 错误 |
| 阴影 | --shadow | rgba(16,24,40,0.06) | rgba(0,0,0,0.28) | 卡片阴影 |

### 规则
- 猎人绿 `--hunter` 只用于可交互元素和激活态标识。不做装饰色。
- 金色 `--gold` 用于勋章、稀有标记、高亮数据。使用频率不超过全局的 5%。
- 所有灰色系偏冷绿调，不和猎人绿冲突。

## 3. 字体

### 字号

| 层级 | 字号 | 字重 | 行高 | tracking | 用途 |
|------|------|------|------|----------|------|
| H1 | 18px | 700 | 1.3 | 0 | 页面标题 |
| H2 | 15px | 700 | 1.4 | 0 | 区块标题 |
| H3 | 13px | 700 | 1.4 | 0 | 卡片标题、侧栏标题 |
| Body | 14px | 400 | 1.55 | 0 | 正文 |
| Body/sm | 13px | 400 | 1.5 | 0 | 次要信息 |
| Caption | 12px | 500 | 1.4 | 0.02em | 标签、元数据 |
| Overline | 11px | 600 | 1.3 | 0.06em | 区块标签（大写） |

### 字体栈
- 主字体：`-apple-system, BlinkMacSystemFont, "Segoe UI", "Microsoft YaHei", sans-serif`
- 等宽字体：`"JetBrains Mono", "Fira Code", "Cascadia Code", monospace`

### 规则
- 正文不低于 13px。
- 数字在数据密集型界面中使用 `font-variant-numeric: tabular-nums`。

## 4. 间距与布局

### 基础单位
所有间距基于 **4px**。

| Token | 值 | 用途 |
|-------|-----|------|
| --space-1 | 4px | 紧凑：图标到文字 |
| --space-2 | 8px | 默认：列表项间距 |
| --space-3 | 12px | 舒适：导航项内边距 |
| --space-4 | 16px | 标准：卡片内边距 |
| --space-5 | 20px | 区块内间距 |
| --space-6 | 24px | 大间隔：区块间 |
| --space-8 | 32px | 分离：卡片组之间 |
| --space-10 | 40px | 页面级分节 |

### 网格
- 三栏布局：sidebar 260px | workspace minmax(0,1fr) | right-panel 1fr
- 1200px 以下收缩为两栏（隐藏右栏）
- 700px 以下单栏堆叠

### Hub 布局

```
+-------------------+-----------------------------+------------------+
| Sidebar (260px)   | Workspace                   | Right Panel      |
| ┌────────────────┐| ┌───────────────────────────┐| ┌──────────────┐ |
| │ Brand          │| │ Topbar                    │| │              │ |
| │ 图标 + 标题    │| │ Title  | actions           │| │ Detail/Edit  │ |
| ├────────────────┤| ├───────────────────────────┤| │ Panel        │ |
| │ Nav            │| │ Content (scroll)          │| │              │ |
| │ · Bot 人设     │| │ ┌───────────────────────┐ │| │              │ |
| │ · 知识库       │| │ │ Section (accent bar)  │ │| │              │ |
| │ · 群管理       │| │ ├───────────────────────┤ │| │              │ |
| ├────────────────┤| │ │ Section               │ │| │              │ |
| │ Bottom Nav     │| │ └───────────────────────┘ │| │              │ |
| │ · 关于猎宝     │| │                           │| │              │ |
| ├────────────────┤| └───────────────────────────┘| └──────────────┘ |
| │ Version        │|                             |                  |
| └────────────────┘|                             |                  |
+-------------------+-----------------------------+------------------+
```

## 5. 组件

### NavItem（导航项）
- **结构**：`button.nav-item[data-view=X]`
- **间距**：`height:36px, padding:0 10px, border-radius:6px`
- **状态**：
  - default：透明背景，`--text-primary` 文字
  - hover：`--surface-secondary` 背景
  - active：`--hunter-soft` 背景 + `--hunter-line` 边框 + 左侧 3px `--hunter` 竖条（通过 `::before` 实现）
  - focus：`--hunter-glow` box-shadow
  - press：`scale(0.97)`
- **Motion**：150ms ease-out

### Card（卡片）
- **结构**：`.card` 容器
- **变体**：
  - 标准：`--surface-elevated` 背景, `--border-default` 边框, `12px border-radius`
  - 玻璃：`.card-glass` — `--surface-glass` 背景, `backdrop-filter: blur(12px)`
  - 扁平：无背景无边框，仅靠 `--surface-primary` 与上层区分
- **状态**：可交互卡片有 hover/focus/active

### Badge（药丸标记）
- **结构**：`span.badge`
- **变体**：default（灰）、success（绿）、warning（金）、error（红）
- **样式**：`inline-flex, align-items:center, gap:4px, border-radius:999px, padding:2px 8px, font-size:11px`

### Toggle（开关）
- **结构**：`.toggle > input[type=checkbox] + span`
- **选中态**：`--hunter` 背景
- **Motion**：滑块 200ms cubic-bezier(0.34, 1.56, 0.64, 1) spring 效果

### Button（按钮）
- **结构**：`button.button`
- **变体**：default、primary（`--hunter`）、ghost（`--surface-secondary`）、danger（`--status-error`）、small
- **状态**：default → hover（border 变亮）→ active（scale 0.97）→ focus（hunter-glow）

## 6. 动效与交互

### 时间

| 类型 | 时长 | 缓动 | 用途 |
|------|------|------|------|
| 微动效 | 100-150ms | ease-out | 按钮点击、开关 |
| 标准 | 200-300ms | ease-in-out | 面板切换、淡入 |
| 强调 | 400-600ms | cubic-bezier(0.16,1,0.3,1) | 侧栏展开 |

### 规则
- 只动画 `transform` 和 `opacity`。
- 每个交互元素都有 hover + active + focus 状态。
- 减少动效：尊重 `prefers-reduced-motion`。

## 7. 深度与表面

### 策略
**混合策略**：以 tonal-shift 为主（表面通过颜色深浅区分层级），辅以微边框。

| 层级 | 表面色 | 边框 |
|------|--------|------|
| 最底层（body） | `--bg-deep` | 无 |
| 表面（sidebar, workspace） | `--surface-primary` | 1px `--border-default` |
| 升起（卡片） | `--surface-secondary` / `--surface-elevated` | 1px `--border-default` |
| 弹窗 | `--surface-elevated` | 1px `--border-default` + shadow |

### 猎人元素
- 侧边栏激活态左侧竖条（3px 宽 `--hunter` 色）
- 金色 `--gold` 用于勋章、数据高亮
- 品牌区十字准心 SVG 图标（`#5db882` 色 1.5px 描边）
- 滚动条用 `--hunter` 色以融入主题
