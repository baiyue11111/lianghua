import type {Field} from '../research/model';
export default function Fields({items}:{items:Field[]}){
 return <dl className="fields">{items.map((field,i)=><div key={i}><dt>{field.name}</dt><dd className={/^[-\d,.%]+(?: [^ ]+)?$/.test(field.value)?"value":""}>{field.value}</dd></div>)}</dl>;
}
