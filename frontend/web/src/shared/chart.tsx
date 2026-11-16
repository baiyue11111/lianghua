import type {ComponentProps, Ref} from 'react';
// `echarts-for-react` already ships a default export, but it pulls in the full
// echarts bundle. The core entry takes the echarts module as a prop, which is
// how the registration list above becomes the only thing that ships. The `esm/`
// build is used directly: `lib/` is CommonJS and resolves to a namespace object
// rather than a component under an ESM bundler.
import Core from 'echarts-for-react/esm/core';
import * as echarts from 'echarts/core';
import {BarChart, CandlestickChart, LineChart} from 'echarts/charts';
import {
  AxisPointerComponent, DataZoomComponent, GridComponent,
  LegendComponent, MarkLineComponent, TooltipComponent,
} from 'echarts/components';
import {CanvasRenderer} from 'echarts/renderers';

// Every chart on every page draws from one registration list, so the bundle
// carries the three series types and six components actually used instead of
// all of echarts. Using a new option feature means adding it here.
echarts.use([
  LineChart, BarChart, CandlestickChart,
  GridComponent, TooltipComponent, LegendComponent,
  DataZoomComponent, AxisPointerComponent, MarkLineComponent,
  CanvasRenderer,
]);

type Props = Omit<ComponentProps<typeof Core>, 'echarts'>;

/** The chart instance, for callers that drive it through `dispatchAction`. */
export type ChartHandle = Core;

export default function Chart({ref, ...rest}: Props & {ref?: Ref<Core>}) {
  return <Core echarts={echarts} {...rest} ref={ref}/>;
}
