import type {BackendPort,UiMessage,UiRequest} from '@display/core';

export function createBackend(fetcher:typeof fetch=fetch):BackendPort {
  return {
    async send(request:UiRequest):Promise<UiMessage[]> {
      const response=await fetcher('/api/protocol',{
        method:'POST',
        headers:{'content-type':'application/json'},
        body:JSON.stringify(request),
      });
      if(!response.ok)throw Error(`后端协议请求失败：${response.status}`);
      const messages:unknown=await response.json();
      if(!Array.isArray(messages))throw Error('后端协议响应必须是消息列表');
      return messages as UiMessage[];
    },
  };
}
