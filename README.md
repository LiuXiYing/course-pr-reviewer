# Course PR Reviewer

面向课程作业仓库的通用 GitHub PR 审核器。每个课程只维护作业配置和学生名单，身份校验、确定性规则、AI、OCR 和视觉审核由一个版本化 Action 复用。

> 当前状态：`0.6.1`。确定性规则、Gemini/GLM 三态文本与图片审核、可选本地 PaddleOCR、PR 评论、自动合并和超期关闭均已实现。

## 设计原则

- 学生身份来自经过教师确认的 GitHub 账号映射，不能由 PR 标题冒充。
- PR 标题仍然严格校验，便于教师直接查看。
- 只有 `PASS` 可以自动合并；不确定结果进入 `MANUAL_REVIEW`。
- GitHub、AI、OCR 或配置异常必须失败关闭。
- 审核哪个 head SHA，就只能合并哪个 SHA。
- 评论使用固定标记幂等更新，不重复刷屏。
- 截止时间使用 PR 首次创建时间；达到关闭阈值后自动评论并关闭。
- 共享审核器不保存课程密钥、学生名单或课程题目。
- 高权限工作流永远不检出或执行学生 PR 中的代码。

本版本不会创建教师兜底标签或自动分派人工任务。`MANUAL_REVIEW` 仍作为安全决策保留，它只会阻止自动通过。

评论、合并和关闭都有独立开关：

```yaml
features:
  auto_merge: true
  comment_review: true
  close_late_pr: true
```

只有 `PASS` 能触发合并。合并和关闭前会重新读取 PR，当前 head SHA 与已审核 SHA 不一致时立即跳过；旧工作流运行不会修改新提交。

## 课程仓库需要维护的文件

```text
.github/
├── course-review.yml
└── students.yml
```

`course-review.yml` 集中保存整个学期的作业配置，每次发布新作业时向 `assignments` 追加一项。`students.yml` 在开学时根据教师确认的 Excel 名单生成，学期中仅在账号变更时更新。

完整示例见：

- [`examples/course-review.yml`](examples/course-review.yml)
- [`examples/students.yml`](examples/students.yml)

## 从 Excel 生成学生名单

Excel 仅使用三列，第一行表头必须精确为：

| 学号 | 姓名 | github账号名 |
|---|---|---|
| 2023010102 | 刘西莹 | example-user |

安装 Excel 导入依赖并转换：

```bash
python -m pip install '.[excel]'
course-pr-reviewer import-students \
  --excel students.xlsx \
  --output .github/students.yml
```

默认不覆盖已有的 `students.yml`。确认覆盖时显式加 `--force`。导入器会拒绝空值、非 10 位学号、重复学号、重复 GitHub 账号、非法 GitHub 账号和多余列。

## 本地验证配置

要求 Python 3.11 或更高版本：

```bash
python -m pip install .
course-pr-reviewer validate \
  --config examples/course-review.yml \
  --students examples/students.yml
```

成功时输出：

```json
{"valid": true, "course": "数据结构", "assignments": ["Lab1", "Lab2"], "students": 2}
```

## 审核决策

| 决策 | 含义 | 自动合并 |
|---|---|:---:|
| `PASS` | 全部检查明确通过 | 是 |
| `FAIL` | 存在有证据的明确违规 | 否 |
| `MANUAL_REVIEW` | 身份未登记、内容有歧义或模型不确定 | 否 |
| `ERROR` | GitHub、配置或外部服务异常 | 否 |

稳定的原因代码定义在 [`models.py`](src/course_pr_reviewer/models.py)，所有运行结果遵守 `review-result.schema.json`。

## Gemini 或 GLM 文本审核

每个阶段通过 `provider` 选择 `gemini` 或 `glm`。审核器使用 JSON 模式，并在本地再用 `ai-review.schema.json` 校验响应。Gemini 文本和图片可共用 `gemini-3.5-flash-lite`。

```yaml
features:
  ai_review: true

ai:
  provider: gemini
  model: gemini-3.5-flash-lite
  min_confidence: 0.8
  timeout_seconds: 60
  max_attempts: 3
  max_file_bytes: 200000
  max_total_bytes: 500000
  max_output_tokens: 2048
```

每个启用 AI 审核的作业必须在 `review_points` 中配置明确的审核点。AI 返回的 `FAIL` 证据必须能在对应学生文件中逐字查到，否则自动降级为 `MANUAL_REVIEW`。低于 `min_confidence` 的结果也会阻止自动通过。

需要看图才能判断的审核点应放进 `vision_review_points`。纯文本阶段只会收到 `review_points`，不会收到图片审核点，也不会收到图片文件名；视觉阶段优先使用 `vision_review_points`，未配置时回退到 `review_points`。把两类审核点混在一起会让文本阶段看到自己无法验证的条件，从而产生无法复核的 `AI_UNCERTAIN`。

启用后，学生提交中的文本/代码内容会发送至所选 API。请按学校数据规则评估是否允许，不要把教师密钥或无关仓库内容加入审核数据。

## 可选 PaddleOCR 与 AI 图片审核

PaddleOCR 在 GitHub Actions runner 本地运行，不需要任何 OCR 密钥。Action 仅在课程配置实际启用 `ocr_review` 时安装 `paddleocr==3.7.0` 和 `paddlepaddle==3.3.1`。首次运行会下载所配置的 OCR 模型。

默认使用体积与准确率较平衡的 `PP-OCRv6_small_det` 和 `PP-OCRv6_small_rec`。OCR 只负责提取截图文字；清洗后的图片和 OCR 结果一起交给所选多模态模型判断。`ocr_review: false` 时不安装 PaddleOCR，Gemini 直接读取图片。

```yaml
features:
  ocr_review: true
  vision_review: true

ocr:
  detection_model: PP-OCRv6_small_det
  recognition_model: PP-OCRv6_small_rec
  min_line_confidence: 0.65
  max_text_chars: 20000

vision:
  provider: gemini
  model: gemini-3.5-flash-lite
  min_confidence: 0.85
  fail_confidence: 0.9
  max_images: 6
  max_image_bytes: 5000000
  max_total_bytes: 12000000
  max_pixels: 25000000
  max_side: 4096

assignments:
  Lab1:
    vision_files:
      - result.*
```

`ocr_review` 必须与 `vision_review` 一起启用。审核器只读取当前 PR head 中精确 blob SHA 对应的图片；图片会先检查格式、像素数、多帧、解压炸弹和大小，再统一转成不含元数据的 PNG。AI 只收到作业内的相对图片文件名，不收到学号或姓名。

图片 `FAIL` 使用更高的 `fail_confidence` 阈值。OCR 类问题的证据还必须能在 PaddleOCR 结果中逐字复核，否则降级为 `MANUAL_REVIEW`。模型或 OCR 服务异常返回 `ERROR`。

Gemini 文本和图片审核共用一个 API Key。它只保存为课程仓库的 GitHub Actions Secret：

```bash
gh secret set GEMINI_API_KEY
```

命令会交互式读取密钥，不要把密钥写在命令行、YAML、代码、PR 或日志中。PaddleOCR 不需要密钥，`PAT_TOKEN` 也不需要。

## 账号与标题规则

审核器先根据 PR 作者查询 `students.yml`，然后生成唯一预期标题：

```text
[{student_id}{student_name}]{assignment_id}作业提交
```

例如：

```text
[2023010102刘西莹]Lab1作业提交
```

- 未登记账号：`MANUAL_REVIEW`
- 登记账号冒用其他学生身份：`FAIL`
- 标题、目录和作业编号不一致：`FAIL`

## 在课程仓库中调用

课程仓库应固定到审核器的 commit SHA，并授予评论、合并和关闭所需的 `pull-requests: write` 与 `contents: write`：

```yaml
- name: Run course reviewer
  uses: LiuXiYing/course-pr-reviewer@REVIEWER_COMMIT_SHA
  with:
    config-path: .github/course-review.yml
    students-path: .github/students.yml
    metadata-dir: pr-info
    github-token: ${{ secrets.GITHUB_TOKEN }}
    gemini-api-key: ${{ secrets.GEMINI_API_KEY }}
```

安全调用分为两个工作流：

1. [`examples/caller-collect.yml`](examples/caller-collect.yml) 在低权限 `pull_request` 中收集可信 PR 元数据。
2. [`examples/caller-review.yml`](examples/caller-review.yml) 在 `workflow_run` 中检出默认分支、重新读取 GitHub API 的当前 PR 数据并运行共享审核器。

高权限阶段不检出、导入或执行学生 PR 内的任何代码。不要在正式课程中引用可移动的 `main` 标签。

## 开发

```bash
python -m pip install -e .
python -m unittest discover -s tests -v
```

CI 在 Python 3.11 和 3.12 上运行全部测试，并验证示例配置。

## 后续阶段

1. 将当前结构化运行元数据汇总成学期统计报告。
