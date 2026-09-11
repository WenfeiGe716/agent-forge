# 参考视频与 Agent Forge v0.15.6 的视觉差异分析

分析日期：2026-09-11。对象为用户提供的约 3 分 8 秒录屏，以及本机实际运行的 Mac 桌面版。

## 结论

差距主要不在“颜色不够接近”或“模型不够精细”，而在于：**当前场景缺少统一的构图、空间识别和对象联动规则。办公室、员工、镜头、姓名牌与任务面板分别成立，但没有稳定地组成同一个视觉体验。**

前一轮先改暖白地面、石墨面板和卡片样式，没有先验证整段视频的空间与交互规律，因此未解决主要差距。这份分析不把前一轮改动当成已经接近参考的证明。

当前最明确的问题是：**点击员工后，员工、镜头和保留的家具使用不同时间的位置，能出现“面板选中 Nova，中央却只剩空桌和卫生间”的画面。** 这是原生应用实际观察与代码共同支持的结论。

![参考视频与当前应用：全景及员工近景对照](/Users/gewenfei/Documents/agent-forge/docs/artifacts/video-gap-2026-09-11/comparison.jpg)

对照图左为参考，右为本机截图；上排比较全景，下排比较选中员工后的构图。图像只裁去边框并等比缩放，没有重绘。两者不是同一布局或同一执行状态，不作像素相似度、字体清晰度或色差评分。

## 一、检查范围与可信边界

- 原视频：1594 × 720，视频流时长 187.803 秒，共 **11,220 帧**；最后一帧时间戳为 187.786056 秒。
- **全量逐帧计算**：解码所有帧，保留原时间戳；裁出主要应用区域，再计算相邻帧的整体、中央、左右区域变化。指标仅用于发现变化，不是审美评分。
- **连续视觉审阅**：每个 0.5 秒窗口选择画面变化最大的帧，并额外保留首尾帧，共 **378 张代表帧、13 张顺序图**。已由主分析与两位独立审阅者分段连续检查，关键画面另行放大。
- **局部逐张原帧审阅**：另外查看 12.9～13.9 秒、107.2～108.2 秒两个镜头变化区间内的全部 **120 张连续原始帧**，检查镜头过渡与人物位置。部分帧与代表帧重合，不将两者相加声称独立覆盖量。
- **当前应用**：打开实际 v0.15.6 桌面版，只读查看全景、点选 Nova、观察移动后的近景，再返回全景；保存两张原生截图。
- **代码对照**：核对组织布局、场景生成、镜头、渲染器、人物/家具、姓名牌、员工面板与页面切换。

准确说，这是“全部帧的计算检查 + 覆盖全片的连续代表帧审阅 + 关键区间逐原帧检查”，**不是声称人工式逐张细看了全部 11,220 帧**。未分析音频；不从模糊的韩文小字推断隐藏功能。

记录文件：

- [逐帧指标 CSV](/Users/gewenfei/Documents/agent-forge/docs/artifacts/video-gap-2026-09-11/all-frame-metrics.csv)
- [代表帧时间戳与文件索引](/Users/gewenfei/Documents/agent-forge/docs/artifacts/video-gap-2026-09-11/selected-frames.json)
- [视频与处理摘要](/Users/gewenfei/Documents/agent-forge/docs/artifacts/video-gap-2026-09-11/summary.json)
- [分析脚本](/Users/gewenfei/Documents/agent-forge/tests/diagnostics/analyze_reference_video.py)
- [对照图与连续帧脚本](/Users/gewenfei/Documents/agent-forge/tests/diagnostics/build_video_comparison.py)

## 二、整段视频实际展示了什么

下列时间是源视频时间戳，不是按截图序号推算。

| 时间段 | 可直接观察的画面 | 对差异分析的意义 |
|---|---|---|
| 00:00～00:06 | 局部工位、拉远、点人物、工位小浮窗之间切换；开头另有播放器覆盖 | 不把录屏播放器当作应用导航，也不把所有镜头跳变都解释为自动相机 |
| 00:06～00:13 | 左侧邀请配置与全景同时存在，彩色工位岛、中央树、低绿植边界清楚 | 参考不是“几乎没有 UI”；它用可识别的空间单元承接 UI |
| 00:13～00:21 | Juno 附近的两个人物、脚下标记、白色气泡、右侧身份与底部人物卡同时出现 | 人物是明确的交互中心；修改配置时仍能看见是谁、在哪里 |
| 00:21～00:29 | 旋转、拉远、再拉近；树、色块、成组桌列保持相对关系 | 缩放不是孤立查看单件模型，而是在同一空间里改变观察尺度 |
| 00:29.900～00:32.616 | 打开设计面板，随后场景整体变暗 | 明亮配色只覆盖部分视频；不能将“暖白”视为全片统一答案 |
| 00:33～00:58 | 深色环境中观察、旋转办公室并输入指令；可见气泡与不同区域 | 日夜不同，但空间标记和部门色映射仍成立 |
| 00:58.534～01:02.750 | 中央三选项决策卡，办公室在模糊暗遮罩后保留 | 这里是模态层变暗，不是又一次日夜切换 |
| 01:03.350～01:14.315 | 回到较稳定全景；工位岛、树、会议桌和道路作为空间参照 | 全景人物本来也不大，不能泛称参考所有画面人物都更大 |
| 01:15.682～01:28.665 | 拉近中央区域，选 Atlas 后转向会议桌，邻近工位与道路保留 | 中景/近景需要保持办公室上下文 |
| 01:29.648～01:41.965 | 会议参与者姿态、气泡和任务文字变化；背景人物、车辆仍活动 | 主事件和背景活动有层次；不是所有对象抢同一注意力 |
| 01:42.533～01:46.847 | 打开三列概览，分别展示目标/工作、成员、流转记录 | 复杂信息另开一层，而非全部塞进人物头顶 |
| 01:47.266～01:59.147 | 关闭概览后 Atlas 移动，随后回到全景，右栏滚动查看信息 | 可追踪到同一人物；无法仅凭视频断定相机是否自动跟随 |
| 02:01.833～02:04.332 | 红色区域的空工位 `quality-s3` 出现小浮窗 | 工位本身是交互对象，不只是家具 |
| 02:06.863～02:10.165 | Atlas 会话浮窗打开并改变位置，右侧身份、底部卡保留 | 会话是员工自己的临时窗口，场景没有离开办公室 |
| 02:16.315～02:30.162 | 切换 Milo，脚下光圈、头顶指示、姓名牌、底部卡、右栏对应同一员工；会话窗移到左侧后关闭 | 关键是对象一致性与浮层位置，不是简单缩小侧栏 |
| 02:30.614～02:42.514 | 拉远、改变角度，街道和外围建筑进入画面 | 参考也有前景建筑遮挡，并非每帧构图都完美 |
| 02:43.399 | 突然回到亮日景，同时时钟、任务计数和表单变化 | 记作明显场景/状态切换，不能当成单独的日夜按钮效果 |
| 02:45.964～03:04.178 | 紫区空座浮窗、邀请选择、Juno 面对面沟通、成员通信范围配置 | 同样的“对象→局部操作→场景继续”模式重复出现 |
| 03:05.478～03:07.786 | 播放器和手机系统控制中心覆盖 | 排除在产品 UI 与场景美术分析之外 |

关键原图：[全景 00:06.053](/Users/gewenfei/Documents/agent-forge/docs/artifacts/video-gap-2026-09-11/frames/0014.jpg)、[人物 00:17.868](/Users/gewenfei/Documents/agent-forge/docs/artifacts/video-gap-2026-09-11/frames/0037.jpg)、[会议 01:28.665](/Users/gewenfei/Documents/agent-forge/docs/artifacts/video-gap-2026-09-11/frames/0179.jpg)、[会话 02:06.863](/Users/gewenfei/Documents/agent-forge/docs/artifacts/video-gap-2026-09-11/frames/0255.jpg)、[Milo 02:29.629](/Users/gewenfei/Documents/agent-forge/docs/artifacts/video-gap-2026-09-11/frames/0301.jpg)、[Juno 02:55.015](/Users/gewenfei/Documents/agent-forge/docs/artifacts/video-gap-2026-09-11/frames/0352.jpg)。

## 三、主要根因：按影响排序

### 1. 镜头与人物脱节，是可复现的具体问题

**观察**：当前点击 Nova 后，右侧选中 Nova；其进入小憩、返回工位等运动状态时，中心画面可以不再看见 Nova，反而出现大块卫生间外壳与空桌。截图保存时右侧状态为“回工位”。

**代码机制**：

- [Office.tsx:53](/Users/gewenfei/Documents/agent-forge/frontend/src/components/Office.tsx:53) 将点击瞬间的员工位置复制为相机目标；不是工位 home。
- [Office.tsx:60](/Users/gewenfei/Documents/agent-forge/frontend/src/components/Office.tsx:60) 550ms 后到达固定目标，后续不继续追踪移动的人物。
- [Office.tsx:50](/Users/gewenfei/Documents/agent-forge/frontend/src/components/Office.tsx:50) 动态可见范围却每帧依据员工新位置判断。
- [Office.tsx:51](/Users/gewenfei/Documents/agent-forge/frontend/src/components/Office.tsx:51) 静态家具裁剪只在控制动作时重新计算。

因此不是单一“镜头不够近”，而是相机位置、静态家具和动态人物三者不同步。**选中了谁，不等于画面正在展示谁。**

参考的 00:13～00:21、02:16～02:30 多次呈现身份卡、脚下圈、姓名牌和镜头对齐到同一人物。它不一定用了自动追踪，但达成了对象可追踪的结果。

应先验证的改进方向：统一对象焦点；明确“跟随员工”和“固定查看工位”的区别；为目标人物和侧栏之间保留可视空间；处理遮挡，不用全局删除邻近环境代替构图。

### 2. 缺少“远看区域、中看团队、近看员工”的尺度设计

**当前事实**：默认正交镜头 `yaw=1.06`、`pitch=.70`、`zoom=.80`，目标 `[3.2,.5,3]`。宽屏的垂直取景范围为 `25/.8=31.25`，不随已布置工位或面板剩余空间调整。[officeRenderer.ts:81](/Users/gewenfei/Documents/agent-forge/frontend/src/graphics/officeRenderer.ts:81)、[officeRenderer.ts:126](/Users/gewenfei/Documents/agent-forge/frontend/src/graphics/officeRenderer.ts:126)

以源码几何在 1280×720 画布投影估算，一个示例坐姿人物约 23×35 CSS 像素、头部约 13×14 像素；这是给定条件下的估算，不是当前截图实测。实际界面中可见大面积道路、地板和文字牌，人物细节很难成为视觉主体。

但参考全景也存在人物很小的时刻。因此正确问题不是“永远把人物放大”，而是**视图切换后该让什么成为主角**。参考经常在团队尺度停留，能同时看见邻座、部门牌和正在互动的人；当前由固定全景直接进入半径裁剪的近景，中间层不充分。

聚焦还会隐藏所有区域标签、停止绘制城市动态，去掉帮助人理解“我现在在哪”的参照。[Office.tsx:63](/Users/gewenfei/Documents/agent-forge/frontend/src/components/Office.tsx:63)、[Office.tsx:77](/Users/gewenfei/Documents/agent-forge/frontend/src/components/Office.tsx:77)

### 3. 区域是可编辑的矩形，但还没有同等成熟的空间美术规则

参考反复出现的是：**成组白色桌列 + 明确的区域色 + 椅背/屏幕色 + 低绿植边界 + 局部标牌/显示屏**。不必先读文字，也能分清几个团队区。树和会议桌是尺度较大的空间锚点。

当前的主要生成方式是：固定 27×20 楼板，在每个区域画边框与浅色平面，再放置同款独立桌椅。多数装饰是在区域角落寻找一个可放置位置；区域颜色只占工作区内层混色的 20%。[departmentOffice.ts:82](/Users/gewenfei/Documents/agent-forge/frontend/src/graphics/departmentOffice.ts:82)、[departmentOffice.ts:95](/Users/gewenfei/Documents/agent-forge/frontend/src/graphics/departmentOffice.ts:95)、[departmentOffice.ts:207](/Users/gewenfei/Documents/agent-forge/frontend/src/graphics/departmentOffice.ts:207)

默认预置为 12 张桌、4 位成员，含 8 个功能区；桌椅全部同向。它保证容量、位置和公共配套，却没有自动产生参考那种共享桌列、入口标识、连续低隔断与视觉密度。[organization_presets.py:58](/Users/gewenfei/Documents/agent-forge/backend/forge/organization_presets.py:58)、[departmentOffice.ts:173](/Users/gewenfei/Documents/agent-forge/frontend/src/graphics/departmentOffice.ts:173)

“为什么感觉不如最早的布局”还有明确代码背景：存在两条场景生成路径。有组织楼层时直接走部门渲染，不再走早期手工场景。早期路径有固定 14 工位位置、中央较大的树/花坛、专门编排的休息和会议区域；部门路径改为根据配置逐区生成。**可编辑性接上了，但早期手工构图的关系没有完整转成可复用布局规则。**[officeWorld.ts:22](/Users/gewenfei/Documents/agent-forge/frontend/src/graphics/officeWorld.ts:22)

这不意味着要取消可编辑楼层，也不能为“热闹”造假员工。应把成组家具、分区标识、通道、绿植与遮挡规则做成设计素材；空桌仍是空桌，真实员工数量仍由用户决定。卫生间应保留，但其外壳不能在普通员工近景里反复成为最显眼的体块。

### 4. 缺的是统一的对象交互，而不只是更少的面板

**必须纠正的误判**：参考也长期保留左侧人员面板、右侧配置和底部输入栏；部分近景左右栏约占应用宽度的一半。不能简单声称“参考 UI 少，所以更好看”。

参考的差异在于：

- 空工位也能直接点，浮窗说明这是哪个座位及下一步动作。
- 员工近景有一致的脚下标记、姓名牌、身份小卡。
- 会话以该员工的窗口出现，可以移动、关闭；依然留在办公室。
- 复杂目标/流转用单独概览层展开，关闭后接回原来的空间。

当前员工任务内容又叠在一套通用员工配置容器里：外层 5 个标签、组织信息、任务摘要/计数、内层 3 个标签、分工、成果、完整任务入口和解释。这些内容各自有用，但缺少一个统一的“此刻最需要做什么”的呈现层。[StudioWorkspace.tsx:65](/Users/gewenfei/Documents/agent-forge/frontend/src/components/StudioWorkspace.tsx:65)、[EmployeeTaskPanel.tsx:59](/Users/gewenfei/Documents/agent-forge/frontend/src/components/EmployeeTaskPanel.tsx:59)

完整成果/任务会切换视图；员工选择和镜头存在办公室组件的本地状态中，组件被卸载后缺少连续恢复机制。[App.tsx:149](/Users/gewenfei/Documents/agent-forge/frontend/src/App.tsx:149)、[StudioWorkspace.tsx:27](/Users/gewenfei/Documents/agent-forge/frontend/src/components/StudioWorkspace.tsx:27)

姓名牌也没有和家具、面板共同避让：当前按屏幕坐标放置，重叠时最多向上挪 5 次，不做场景深度或侧栏遮挡判断。[Office.tsx:73](/Users/gewenfei/Documents/agent-forge/frontend/src/components/Office.tsx:73)

改进不是在所有员工头上再加更多卡片。应让简短状态、需要人决定的动作、通信与成果分别有对应的轻量入口；复杂内容展开时仍保留员工、任务和空间上下文。每个动作仍需绑定具体任务/申请，不能变成对某员工的永久授权。

### 5. 微细节不少，但整体造型、配色与光影没有统一到展示尺度

当前家具已包含独立键帽、显示器背部通风口、杯把、笔、椅轮等；人物也有细化身体与动作部件。问题不能概括为“全部都是没有细节的方块”。[furniture.ts:35](/Users/gewenfei/Documents/agent-forge/frontend/src/graphics/furniture.ts:35)、[furniture.ts:50](/Users/gewenfei/Documents/agent-forge/frontend/src/graphics/furniture.ts:50)

但在全景里，最醒目的是桌面轮廓、椅背、屏幕、成组关系和区域边界。当前大多看到同方向的屏幕背板；键盘细节被缩小，无法弥补宏观轮廓的重复。参考里甚至有很简洁的白色人形，其优势是轮廓、位置和交互明确，不是写实度更高。

光照方面，当前有方向光、半球环境光和阴影贴图，不是“没有阴影”。但没有空间环境遮蔽与间接照明计算；阴影覆盖固定大范围，近景放大不会同步收紧范围。明亮大地板和附近家具的接触层次有限。[officeRenderer.ts:42](/Users/gewenfei/Documents/agent-forge/frontend/src/graphics/officeRenderer.ts:42)、[officeRenderer.ts:95](/Users/gewenfei/Documents/agent-forge/frontend/src/graphics/officeRenderer.ts:95)

参考的具体引擎、面数、光照技术不能由录屏确认。仅凭它“看着柔和”就说使用了某种高级渲染技术，不可靠。

界面颜色还有多轮覆盖：新样式使用石墨与鼠尾草绿，旧提交按钮仍保留紫色渐变，字号与宽度规则叠加。参考更稳定的是“同一部门颜色贯穿空间和身份标记”，而非所有按钮统一换某一种颜色。[employee-task.css:25](/Users/gewenfei/Documents/agent-forge/frontend/src/employee-task.css:25)、[studio.css:9](/Users/gewenfei/Documents/agent-forge/frontend/src/studio.css:9)

### 6. 活动的组织方式，比单纯增加动作数量更重要

参考会议段区分白色发言气泡、带成员指向的任务文字、会话记录与背景活动。当前虽然已经有真实事件驱动的会议、消息和空闲动作，但最近消息气泡、行为状态和日志的表现尚未形成同样清楚的层次。

当前行走主要使用程序摆腿，部分状态切换直接换姿态；相机缓动只覆盖位置/缩放，方向直接改变；渲染循环有 32ms 间隔限制。这些是需专项检查的动作/过渡约束，不等于已经量化证明比参考低多少 FPS。[Office.tsx:53](/Users/gewenfei/Documents/agent-forge/frontend/src/components/Office.tsx:53)、[Office.tsx:55](/Users/gewenfei/Documents/agent-forge/frontend/src/components/Office.tsx:55)、[characters.ts:96](/Users/gewenfei/Documents/agent-forge/frontend/src/graphics/characters.ts:96)

视频约 59.7 的平均录制帧率，不代表参考产品实际持续渲染 59.7 FPS；本次没有做同机性能基准。不能把录屏帧率直接作为性能差距证据。

当前实际运行已结束、部分成员无任务，画面缺少工作气泡是合理状态，不能与视频正在演示会议的画面直接对比“热闹程度”，更不能自动生成聊天来补视觉。后续动态验收需要明确标识的测试场景或获准的真实任务。

## 四、哪些结论不成立

1. **“换成 Mac 应用就自然更好看。”** 当前已是 Mac 桌面版，但画面仍由当前场景与界面代码决定；外壳不解决构图问题。
2. **“把所有面板缩小就会像参考。”** 参考面板也不少，重要的是对象联动、层次和位置。
3. **“继续加模型面数、加键盘细节。”** 现有微细节已经超出很多全景下的可读尺度，应先解决轮廓和像素占比。
4. **“全部换暖白色。”** 视频包含亮日景、暗环境、模态遮罩与场景跳转，不能只取一种色调代表它。
5. **“参考一定真实执行了模型与工具。”** 视频能证明呈现方式，不能证明请求、权限、文件产物或消息真实性。
6. **“参考每个镜头都没有遮挡。”** 后段同样有前景建筑遮挡；应学习其稳定优点，不照搬缺陷。

## 五、正确的后续优先顺序与验收方式

以下是分析得出的建议，**本次未实施**。

| 顺序 | 工作重点 | 应通过的可见验收 |
|---|---|---|
| 1 | 相机、对象焦点和遮挡同步 | 员工走向会议/茶水区后仍可定位；不再出现选中某人却只看空桌；拖动镜头时能明确退出跟随 |
| 2 | 楼层的成组空间设计 | 远景能凭形状、色块、标牌分清团队；中景保留邻座与区域；近景不被卫生间外壳抢占 |
| 3 | 员工/工位/任务的统一交互 | 点对象、开会话、处理明确申请、查看成果、返回办公室时，选中对象与具体任务持续一致 |
| 4 | 轮廓、材质、光照与颜色映射 | 在真实常用缩放下审查桌列、人物、区域色和接触阴影，不用超近景细节代替全景验收 |
| 5 | 动作和多人并行信息 | 分清实际任务、消息、待审批和空闲动作；复杂任务可展开，多个任务不混用进度/申请 |

验收应固定几组状态：默认全景、选中在座员工、选中移动员工、会议多人近景、明确待审批事项、查看员工通信/成果，以及返回同一场景。每组保存截图与短录屏，并同时验证真实任务边界。不能只靠组件测试通过，或只挑一张好看的静态截图。

## 本次改动边界

仅新增分析脚本、视频派生图像、逐帧指标和本报告；应用源码与运行配置未继续修改。没有新增/启动任务、调用模型、批准申请、变更成员/工位，也没有为视觉补假事件。源视频未修改、未上传。
