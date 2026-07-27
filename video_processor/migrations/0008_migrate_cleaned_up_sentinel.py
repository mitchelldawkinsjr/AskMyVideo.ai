"""Replace the '[CLEANED_UP]' video_path sentinel with file_removed_at."""

from django.db import migrations
from django.utils import timezone

SENTINEL = " [CLEANED_UP]"


def forwards(apps, schema_editor):
    VideoJob = apps.get_model("video_processor", "VideoJob")
    for job in VideoJob.objects.filter(video_path__endswith=SENTINEL):
        job.video_path = job.video_path[: -len(SENTINEL)]
        job.file_removed_at = timezone.now()
        job.save(update_fields=["video_path", "file_removed_at"])


class Migration(migrations.Migration):

    dependencies = [
        ("video_processor", "0007_videojob_file_removed_at_and_more"),
    ]

    operations = [
        migrations.RunPython(forwards, migrations.RunPython.noop),
    ]
