# Copyright: (c) OpenSpug Organization. https://github.com/openspug/spug
# Copyright: (c) <spug.dev@gmail.com>
# Released under the AGPL-3.0 License.
from io import StringIO

from django.views.generic import View
from django.db.models import F
from django.http.response import HttpResponseBadRequest
from libs import json_response, JsonParser, Argument, AttrDict, auth
from apps.setting.utils import AppSetting
from apps.account.utils import get_host_perms
from apps.host.models import Host, Group
from apps.host.utils import batch_sync_host, _sync_host_extend
from apps.exec.models import ExecTemplate
from apps.app.models import Deploy
from apps.schedule.models import Task
from apps.monitor.models import Detection
from libs.ssh import SSH, AuthenticationException
from paramiko.ssh_exception import BadAuthenticationType
from openpyxl import load_workbook
from threading import Thread
import socket
import uuid
import paramiko
import json
import os
from django.views.generic import View
from django.http import StreamingHttpResponse, JsonResponse
from apps.host.models import Host
# 处理中文文件名编码
from django.utils.encoding import escape_uri_path
import traceback
from apps.setting.utils import AppSetting
import json
import paramiko
import stat
from datetime import datetime
from io import StringIO
from django.views.generic import View
from django.http import JsonResponse
from apps.host.models import Host
from apps.setting.utils import AppSetting



class HostView(View):
    def get(self, request):
        hosts = Host.objects.select_related('hostextend')
        if not request.user.is_supper:
            hosts = hosts.filter(id__in=get_host_perms(request.user))
        hosts = {x.id: x.to_view() for x in hosts}
        for rel in Group.hosts.through.objects.filter(host_id__in=hosts.keys()):
            hosts[rel.host_id]['group_ids'].append(rel.group_id)
        return json_response(list(hosts.values()))

    @auth('host.host.add|host.host.edit')
    def post(self, request):
        form, error = JsonParser(
            Argument('id', type=int, required=False),
            Argument('group_ids', type=list, filter=lambda x: len(x), help='请选择主机分组'),
            Argument('name', help='请输主机名称'),
            Argument('username', handler=str.strip, help='请输入登录用户名'),
            Argument('hostname', handler=str.strip, help='请输入主机名或IP'),
            Argument('port', type=int, help='请输入SSH端口'),
            Argument('pkey', required=False),
            Argument('desc', required=False),
            Argument('password', required=False),
        ).parse(request.body)
        if error is None:
            if not _do_host_verify(form):
                return json_response('auth fail')

            group_ids = form.pop('group_ids')
            other = Host.objects.filter(name=form.name).first()
            if other and (not form.id or other.id != form.id):
                return json_response(error=f'已存在的主机名称【{form.name}】')
            if form.id:
                Host.objects.filter(pk=form.id).update(is_verified=True, **form)
                host = Host.objects.get(pk=form.id)
            else:
                host = Host.objects.create(created_by=request.user, is_verified=True, **form)
            host.groups.set(group_ids)
            response = host.to_view()
            response['group_ids'] = group_ids
            return json_response(response)
        return json_response(error=error)

    @auth('host.host.add|host.host.edit')
    def put(self, request):
        form, error = JsonParser(
            Argument('id', type=int, help='参数错误')
        ).parse(request.body)
        if error is None:
            host = Host.objects.get(pk=form.id)
            with host.get_ssh() as ssh:
                _sync_host_extend(host, ssh=ssh)
        return json_response(error=error)

    @auth('admin')
    def patch(self, request):
        form, error = JsonParser(
            Argument('host_ids', type=list, filter=lambda x: len(x), help='请选择主机'),
            Argument('s_group_id', type=int, help='参数错误'),
            Argument('t_group_id', type=int, help='参数错误'),
            Argument('is_copy', type=bool, help='参数错误'),
        ).parse(request.body)
        if error is None:
            if form.t_group_id == form.s_group_id:
                return json_response(error='不能选择本分组的主机')
            s_group = Group.objects.get(pk=form.s_group_id)
            t_group = Group.objects.get(pk=form.t_group_id)
            t_group.hosts.add(*form.host_ids)
            if not form.is_copy:
                s_group.hosts.remove(*form.host_ids)
        return json_response(error=error)

    @auth('host.host.del')
    def delete(self, request):
        form, error = JsonParser(
            Argument('id', type=int, required=False),
            Argument('group_id', type=int, required=False),
        ).parse(request.GET)
        if error is None:
            if form.id:
                host_ids = [form.id]
            elif form.group_id:
                group = Group.objects.get(pk=form.group_id)
                host_ids = [x.id for x in group.hosts.all()]
            else:
                return json_response(error='参数错误')
            for host_id in host_ids:
                regex = fr'[^0-9]{host_id}[^0-9]'
                deploy = Deploy.objects.filter(host_ids__regex=regex) \
                    .annotate(app_name=F('app__name'), env_name=F('env__name')).first()
                if deploy:
                    return json_response(error=f'应用【{deploy.app_name}】在【{deploy.env_name}】的发布配置关联了该主机，请解除关联后再尝试删除该主机')
                task = Task.objects.filter(targets__regex=regex).first()
                if task:
                    return json_response(error=f'任务计划中的任务【{task.name}】关联了该主机，请解除关联后再尝试删除该主机')
                detection = Detection.objects.filter(type__in=('3', '4'), targets__regex=regex).first()
                if detection:
                    return json_response(error=f'监控中心的任务【{detection.name}】关联了该主机，请解除关联后再尝试删除该主机')
                tpl = ExecTemplate.objects.filter(host_ids__regex=regex).first()
                if tpl:
                    return json_response(error=f'执行模板【{tpl.name}】关联了该主机，请解除关联后再尝试删除该主机')
            Host.objects.filter(id__in=host_ids).delete()
        return json_response(error=error)


@auth('host.host.add')
def post_import(request):
    group_id = request.POST.get('group_id')
    file = request.FILES['file']
    hosts = []
    ws = load_workbook(file, read_only=True)['Sheet1']
    summary = {'fail': 0, 'success': 0, 'invalid': [], 'skip': [], 'repeat': []}
    for i, row in enumerate(ws.rows, start=1):
        if i == 1:  # 第1行是表头 略过
            continue
        if not all([row[x].value for x in range(4)]):
            summary['invalid'].append(i)
            summary['fail'] += 1
            continue
        data = AttrDict(
            name=row[0].value,
            hostname=row[1].value,
            port=row[2].value,
            username=row[3].value,
            desc=row[5].value
        )
        if Host.objects.filter(hostname=data.hostname, port=data.port, username=data.username).exists():
            summary['skip'].append(i)
            summary['fail'] += 1
            continue
        if Host.objects.filter(name=data.name).exists():
            summary['repeat'].append(i)
            summary['fail'] += 1
            continue
        host = Host.objects.create(created_by=request.user, **data)
        host.groups.add(group_id)
        summary['success'] += 1
        host.password = row[4].value
        hosts.append(host)
    token = uuid.uuid4().hex
    if hosts:
        Thread(target=batch_sync_host, args=(token, hosts)).start()
    return json_response({'summary': summary, 'token': token, 'hosts': {x.id: {'name': x.name} for x in hosts}})


@auth('host.host.add')
def post_parse(request):
    file = request.FILES['file']
    if file:
        data = file.read()
        return json_response(data.decode())
    else:
        return HttpResponseBadRequest()


@auth('host.host.add')
def batch_valid(request):
    form, error = JsonParser(
        Argument('password', required=False),
        Argument('range', filter=lambda x: x in ('1', '2'), help='参数错误')
    ).parse(request.body)
    if error is None:
        if form.range == '1':  # all hosts
            hosts = Host.objects.all()
        else:
            hosts = Host.objects.filter(is_verified=False).all()
        token = uuid.uuid4().hex
        Thread(target=batch_sync_host, args=(token, hosts, form.password)).start()
        return json_response({'token': token, 'hosts': {x.id: {'name': x.name} for x in hosts}})
    return json_response(error=error)


def _do_host_verify(form):
    password = form.pop('password')
    if form.pkey:
        try:
            with SSH(form.hostname, form.port, form.username, form.pkey) as ssh:
                ssh.ping()
            return True
        except BadAuthenticationType:
            raise Exception('该主机不支持密钥认证，请参考官方文档，错误代码：E01')
        except AuthenticationException:
            raise Exception('上传的独立密钥认证失败，请检查该密钥是否能正常连接主机（推荐使用全局密钥）')
        except socket.timeout:
            raise Exception('连接主机超时，请检查网络')

    private_key, public_key = AppSetting.get_ssh_key()
    if password:
        try:
            with SSH(form.hostname, form.port, form.username, password=password) as ssh:
                ssh.add_public_key(public_key)
        except BadAuthenticationType:
            raise Exception('该主机不支持密码认证，请参考官方文档，错误代码：E00')
        except AuthenticationException:
            raise Exception('密码连接认证失败，请检查密码是否正确')
        except socket.timeout:
            raise Exception('连接主机超时，请检查网络')

    try:
        with SSH(form.hostname, form.port, form.username, private_key) as ssh:
            ssh.ping()
    except BadAuthenticationType:
        raise Exception('该主机不支持密钥认证，请参考官方文档，错误代码：E01')
    except AuthenticationException:
        if password:
            raise Exception('密钥认证失败，请参考官方文档，错误代码：E02')
        return False
    except socket.timeout:
        raise Exception('连接主机超时，请检查网络')
    return True


class HostFileDownloadView(View):
    @auth('host.host.view')
    def post(self, request):
        ssh = None
        sftp = None
        try:
            data = json.loads(request.body)
            host_id = data.get('host_id')
            file_path = data.get('file_path')

            if not host_id or not file_path:
                return JsonResponse({'error': '参数缺失'}, status=400)

            # 1. 查询主机
            host = Host.objects.filter(pk=host_id).first()
            if not host:
                return JsonResponse({'error': '主机不存在'}, status=404)

            # 2. 获取连接用的私钥 (核心修改逻辑) 🔑
            # 优先用主机独立的私钥，如果没有，就用系统全局私钥
            pkey_str = host.pkey
            if not pkey_str:
                print(f">>>> [调试] 主机没有独立密钥，尝试获取全局密钥...")
                pkey_str = AppSetting.get('private_key')

            if not pkey_str:
                return JsonResponse({'error': '无法连接：未找到主机密钥，也未配置全局密钥'}, status=500)

            # 3. 创建 SSH 密钥对象
            try:
                pkey = paramiko.RSAKey.from_private_key(StringIO(pkey_str))
            except Exception as e:
                return JsonResponse({'error': f'私钥格式错误: {str(e)}'}, status=500)

            # 4. 建立 SSH 连接
            # Spug 的逻辑是：有密钥就只用密钥，不看密码
            ssh = paramiko.SSHClient()
            ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())

            print(f">>>> [调试] 正在连接... IP:{host.hostname} User:{host.username}")

            ssh.connect(
                hostname=host.hostname,
                port=host.port,
                username=host.username,
                pkey=pkey,  # 👈 只传密钥
                timeout=10,
                look_for_keys=False,  # 禁止自动搜索本地 ~/.ssh/ 目录
                allow_agent=False  # 禁止使用 SSH 代理
            )

            # 5. 开启 SFTP 下载
            sftp = ssh.open_sftp()

            try:
                sftp.stat(file_path)
            except FileNotFoundError:
                sftp.close()
                ssh.close()
                return JsonResponse({'error': f'远程文件不存在: {file_path}'}, status=404)

            remote_file = sftp.open(file_path, 'rb')

            def file_iterator(file_obj, chunk_size=8192):
                try:
                    while True:
                        data = file_obj.read(chunk_size)
                        if not data:
                            break
                        yield data
                finally:
                    # 确保流传输结束或中断时关闭连接
                    if file_obj: file_obj.close()
                    if sftp: sftp.close()
                    if ssh: ssh.close()
                    print(">>>> [调试] 连接已关闭")

            filename = os.path.basename(file_path)
            response = StreamingHttpResponse(file_iterator(remote_file))
            response['Content-Type'] = 'application/octet-stream'
            response['Content-Disposition'] = f'attachment; filename="{escape_uri_path(filename)}"'
            return response

        except Exception as e:
            traceback.print_exc()
            # 如果连接还没建立成功就报错，手动关闭资源
            if sftp: sftp.close()
            if ssh: ssh.close()
            return JsonResponse({'error': f"系统异常: {str(e)}"}, status=500)

class HostFileListView(View):
            def post(self, request):
                ssh = None
                sftp = None
                try:
                    data = json.loads(request.body)
                    host_id = data.get('host_id')
                    # 如果没传路径，默认去根目录 /
                    path = data.get('path', '/')

                    if not host_id:
                        return JsonResponse({'error': '参数缺失'}, status=400)

                    # 1. 获取主机和密钥 (复用之前的逻辑)
                    host = Host.objects.filter(pk=host_id).first()
                    if not host:
                        return JsonResponse({'error': '主机不存在'}, status=404)

                    pkey_str = host.pkey
                    if not pkey_str:
                        pkey_str = AppSetting.get('private_key')

                    if not pkey_str:
                        return JsonResponse({'error': '未找到有效密钥'}, status=500)

                    try:
                        pkey = paramiko.RSAKey.from_private_key(StringIO(pkey_str))
                    except Exception as e:
                        return JsonResponse({'error': f'密钥格式错误: {str(e)}'}, status=500)

                    # 2. 连接 SSH
                    ssh = paramiko.SSHClient()
                    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                    ssh.connect(
                        hostname=host.hostname,
                        port=host.port,
                        username=host.username,
                        pkey=pkey,
                        timeout=5,
                        look_for_keys=False,
                        allow_agent=False
                    )

                    sftp = ssh.open_sftp()

                    # 3. 获取文件列表 (核心逻辑)
                    # 类似于 Java 的 File.listFiles()
                    try:
                        # listdir_attr 可以获取文件属性（大小、时间等）
                        file_attrs = sftp.listdir_attr(path)
                    except IOError:
                        return JsonResponse({'error': f'无法访问路径: {path} (可能是权限不足或路径不存在)'}, status=400)

                    result = []
                    for attr in file_attrs:
                        # 过滤掉 . 和 ..
                        if attr.filename in ['.', '..']:
                            continue

                        # 判断是文件夹还是文件
                        is_dir = stat.S_ISDIR(attr.st_mode)
                        # 如果是链接，也可以视为文件或文件夹，这里简单处理，链接也当文件展示或者根据实际指向判断
                        # 简单起见，我们只标记是否为文件夹

                        result.append({
                            'name': attr.filename,
                            'is_dir': is_dir,
                            'size': attr.st_size,  # 字节单位
                            'modify_time': datetime.fromtimestamp(attr.st_mtime).strftime('%Y-%m-%d %H:%M:%S')
                        })

                    # 4. 排序：文件夹排前面，文件排后面，按名称排序
                    # 这是一个很贴心的细节，前端展示会更好看
                    result.sort(key=lambda x: (not x['is_dir'], x['name']))

                    return JsonResponse({'data': result, 'current_path': path})

                except Exception as e:
                    return JsonResponse({'error': str(e)}, status=500)
                finally:
                    if sftp: sftp.close()
                    if ssh: ssh.close()