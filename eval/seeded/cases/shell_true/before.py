"""Report export helper."""
import subprocess


def export_report(report_id, dest_dir):
    subprocess.run(
        ["pdfgen", "--id", str(report_id), "--out", dest_dir],
        check=True,
    )
    return f"{dest_dir}/{report_id}.pdf"
