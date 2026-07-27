from pathlib import Path

from django.db import migrations, models


AUDIO_EXTENSIONS = {".mp3", ".wav", ".m4a", ".aac"}
DOCUMENT_EXTENSIONS = {".pdf"}


def backfill_content_kind(apps, schema_editor):
    VideoJob = apps.get_model("video_processor", "VideoJob")
    for job in VideoJob.objects.all().iterator():
        if job.youtube_url:
            kind = "video"
        else:
            extension = Path(job.video_path or job.video_name or "").suffix.lower()
            if extension in DOCUMENT_EXTENSIONS:
                kind = "document"
            elif extension in AUDIO_EXTENSIONS:
                kind = "audio"
            else:
                kind = "video"
        if job.content_kind != kind:
            job.content_kind = kind
            job.save(update_fields=["content_kind"])


class Migration(migrations.Migration):

    dependencies = [
        ("video_processor", "0008_migrate_cleaned_up_sentinel"),
    ]

    operations = [
        migrations.AddField(
            model_name="videojob",
            name="content_kind",
            field=models.CharField(
                choices=[
                    ("video", "Video"),
                    ("audio", "Audio"),
                    ("document", "Document"),
                ],
                default="video",
                help_text="video, audio, or document",
                max_length=20,
            ),
        ),
        migrations.RunPython(backfill_content_kind, migrations.RunPython.noop),
    ]
