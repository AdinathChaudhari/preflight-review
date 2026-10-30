"""Report export helper."""
import subprocess


def export_report(report_id, dest_dir):
    subprocess.run(
        f"pdfgen --id {report_id} --out {dest_dir}",
        shell=True,
        check=True,
    )
    return f"{dest_dir}/{report_id}.pdf"
