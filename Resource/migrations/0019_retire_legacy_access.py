from django.db import migrations


def revoke_existing_shares(apps, schema_editor):
    apps.get_model('Resource', 'Share').objects.using(schema_editor.connection.alias).all().delete()


class Migration(migrations.Migration):
    dependencies = [('Resource', '0018_share_resource_note')]

    operations = [
        migrations.RunPython(revoke_existing_shares),
        migrations.DeleteModel(name='UserRight'),
        migrations.RemoveField(model_name='resource', name='description'),
        migrations.RemoveField(model_name='resource', name='right_bubble'),
        migrations.RemoveField(model_name='resource', name='status'),
        migrations.RemoveField(model_name='resource', name='visit_key'),
        migrations.RemoveField(model_name='resource', name='vk_change_time'),
    ]
