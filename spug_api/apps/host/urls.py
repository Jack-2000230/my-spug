# Copyright: (c) OpenSpug Organization. https://github.com/openspug/spug
# Copyright: (c) <spug.dev@gmail.com>
# Released under the AGPL-3.0 License.
from django.urls import path

from apps.host.views import *
from apps.host.group import GroupView
from apps.host.extend import ExtendView
from apps.host.add import get_regions, cloud_import
print(">>>>>> [系统启动] Host URL路由正在加载！ <<<<<<")
urlpatterns = [
    path('', HostView.as_view()),
    path('extend/', ExtendView.as_view()),
    path('group/', GroupView.as_view()),
    path('import/', post_import),
    path('import/cloud/', cloud_import),
    path('import/region/', get_regions),
    path('parse/', post_parse),
    path('valid/', batch_valid),
    path('file/download/', HostFileDownloadView.as_view()),
    path('file/list/', HostFileListView.as_view()),
]
