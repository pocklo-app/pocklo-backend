import uuid

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [('Resource', '0017_alter_resource_options_alter_userright_options_and_more')]

    operations = [
        migrations.AlterField(
            model_name='resource',
            name='status',
            field=models.IntegerField(
                choices=[(0, 0), (1, 1), (2, 2)],
                default=1, verbose_name='加密状态 0公开 1仅自己可见 2需要密码',
            ),
        ),
        migrations.AlterField(
            model_name='resource',
            name='right_bubble',
            field=models.BooleanField(blank=True, default=False, null=True, verbose_name='读取权限向上冒泡'),
        ),
        migrations.AlterField(
            model_name='resource',
            name='description',
            field=models.TextField(blank=True, default=None, null=True, verbose_name='resource note'),
        ),
        migrations.CreateModel(
            name='Share',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('token', models.UUIDField(default=uuid.uuid4, editable=False, unique=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('resource', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='shares',
                    to='Resource.resource',
                )),
            ],
        ),
    ]
