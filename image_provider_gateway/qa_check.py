from __future__ import annotations

from .models import ImageResult, QaIssue, QaResult


def qa_check_image(result: ImageResult, preset: str = "basic") -> QaResult:
    """Run the gateway's built-in technical QA.

    The gateway default remains generic: it checks file/format/size metadata only.
    Business or visual QA should be implemented by upper-layer Skills or external
    QA providers using files in `qa_presets/` as rule presets.
    """
    issues: list[QaIssue] = []
    if not result.path:
        issues.append(
            QaIssue(
                severity="severe_warning",
                category="file_integrity",
                message="没有记录输出图片路径。",
                suggestion="建议检查 provider 返回与保存流程，必要时重新生成。",
            )
        )
    if result.aspect_ratio_ok is False:
        issues.append(
            QaIssue(
                severity="warning",
                category="aspect_ratio",
                message="实际图片比例与请求 size 比例不一致。",
                suggestion="如比例影响使用，可由用户确认后重试该图。",
            )
        )
    if result.format_detected == "UNKNOWN":
        issues.append(
            QaIssue(
                severity="severe_warning",
                category="image_format",
                message="图片格式无法识别。",
                suggestion="建议检查文件或由用户确认后重试该图。",
            )
        )
    if not result.actual_size:
        issues.append(
            QaIssue(
                severity="warning",
                category="image_metadata",
                message="未能记录实际图片尺寸。",
                suggestion="建议检查图片探测逻辑或人工确认输出文件。",
            )
        )
    if issues:
        status = "severe_warning" if any(issue.severity == "severe_warning" for issue in issues) else "warning"
        return QaResult(status=status, issues=issues, note="Basic technical QA only; warnings do not trigger automatic retry.", preset=preset)
    return QaResult(status="pass", issues=[], note="Basic technical QA passed.", preset=preset)
