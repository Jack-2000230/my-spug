# Copyright: (c) OpenSpug Organization. https://github.com/openspug/spug
# Copyright: (c) <spug.dev@gmail.com>
# Released under the AGPL-3.0 License.
from django.utils.deprecation import MiddlewareMixin
from django.conf import settings
from .utils import json_response, get_request_real_ip
from apps.account.models import User
from apps.setting.utils import AppSetting
import traceback
import time


class HandleExceptionMiddleware(MiddlewareMixin):
    """
    处理试图函数异常
    """

    def process_exception(self, request, exception):
        traceback.print_exc()
        return json_response(error='Exception: %s' % exception)


class AuthenticationMiddleware(MiddlewareMixin):
    """
    登录验证
    """

    def process_request(self, request):
        if request.path in settings.AUTHENTICATION_EXCLUDES:
            return None
        if any(x.match(request.path) for x in settings.AUTHENTICATION_EXCLUDES if hasattr(x, 'match')):
            return None

        access_token = request.headers.get('x-token') or request.GET.get('x-token')

        # =========== 👇 调试代码开始 👇 ===========
        print(f"\n>>>> [中间件] 收到 Token: {access_token}")
        if access_token and len(access_token) == 32:
            x_real_ip = get_request_real_ip(request.headers)
            user = User.objects.filter(access_token=access_token).first()

            print(f">>>> [中间件] 查库用户: {user}")
            if user:
                import time
                now = time.time()
                print(
                    f">>>> [中间件] Token过期时间: {user.token_expired} | 当前时间: {now} | 是否过期: {user.token_expired < now}")
                print(f">>>> [中间件] 用户IP: {user.last_ip} | 请求IP: {x_real_ip}")

                bind_ip_setting = AppSetting.get_default('bind_ip')
                print(f">>>> [中间件] 系统设置 bind_ip: {bind_ip_setting}")

                # 原有逻辑
                if user.token_expired >= time.time() and user.is_active:
                    # if x_real_ip == user.last_ip or bind_ip_setting is False:
                    if True:
                        request.user = user
                        user.token_expired = time.time() + settings.TOKEN_TTL
                        user.save()
                        return None
                    else:
                        print(">>>> [中间件失败] IP地址不匹配！")
                else:
                    print(">>>> [中间件失败] Token已过期 或 账号未激活")
            else:
                print(">>>> [中间件失败] 数据库找不到该 Token 对应的用户")
        else:
            print(f">>>> [中间件失败] Token 长度不对或为空: {len(access_token) if access_token else 0}")
        # =========== 👆 调试代码结束 👆 ===========

        response = json_response(error="验证失败，请重新登录")
        response.status_code = 401
        return response
