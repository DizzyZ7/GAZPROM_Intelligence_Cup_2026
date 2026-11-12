#!/usr/bin/env python3
"""Маржрут: воспроизводимая офлайн-аналитика конкурсного XLSB.

Не требует pandas, pyxlsb или внешних API. Python >=3.11, numpy.
Выводит только агрегированные метрики; исходные клиентские ID не экспортирует.
"""
from __future__ import annotations
import argparse, collections, csv, json
from pathlib import Path
from zipfile import ZipFile
import numpy as np
from xlsb_reader import get_book, sheet_rows
MONTHS=['2023-09','2023-10','2023-11','2023-12','2024-01','2024-02','2024-03','2024-04','2024-05']
MIN_PEERS=8; MIN_ACTIVE_MONTHS=6; MIN_VOLUME_L=200; MIN_SPAN_RUB=1.0; SHRINKAGE_PENALTY=4.0

def read_source(path:Path):
    if not path.is_file():
        raise FileNotFoundError(f'Исходная конкурсная книга не найдена: {path}')
    if path.stat().st_size > 80 * 1024 * 1024:
        raise ValueError('Входной XLSB превышает лимит 80 МиБ')
    with ZipFile(path) as z:
        infos = z.infolist()
        if len(infos) > 200 or sum(info.file_size for info in infos) > 200 * 1024 * 1024:
            raise ValueError('Входная книга превышает допустимый размер после распаковки')
        if any(info.flag_bits & 1 for info in infos):
            raise ValueError('Зашифрованные XLSB-файлы не поддерживаются')
        sheet_names,shared=get_book(z)
        want=['Данные','Информация об АЗС','Информация о клиентах','Покупки сопутствующих товаров','Маржа','Плотность']
        if sheet_names!=want:raise ValueError(f'Unexpected worksheet schema: {sheet_names!r}')
        rows=[]
        for idx,row in sheet_rows(z,1,shared):
            if idx<3:continue
            d=dict(row)
            if any(k not in d for k in range(30)): raise ValueError(f'Incomplete base row {idx+1}: {sorted(d)}')
            rows.append((str(d[0]),str(d[1]),str(d[2]),*[float(d[k]) for k in range(3,30)]))
        stations={};clients={};nonfuel={};margin={};densities={}
        for idx,row in sheet_rows(z,2,shared):
            if idx: d=dict(row);stations[str(d[0])]=d
        for idx,row in sheet_rows(z,3,shared):
            if idx:d=dict(row);clients[str(d[0])]=d
        for idx,row in sheet_rows(z,4,shared):
            if idx:d=dict(row);nonfuel[str(d[0])]=d
        for idx,row in sheet_rows(z,5,shared):
            if idx:
                d=dict(row);margin[(norm_fuel(str(d[0])),str(d[1]))]=(float(d[2]),float(d[3]))
        for idx,row in sheet_rows(z,6,shared):
            if idx:d=dict(row);densities[norm_fuel(str(d[0]))]=float(d[1])
    return rows,stations,clients,nonfuel,margin,densities

def norm_fuel(s):
    return s.strip().upper()

def segment_one(beta:float, eligible:bool):
    if not eligible:return 'Недостаточно данных'
    if beta <= -0.08:return 'Высокий сигнал'
    if beta <= -0.025:return 'Умеренный сигнал'
    return 'Не выявлен'

def stable_corr(a,b):
    a=np.asarray(a,dtype=float);b=np.asarray(b,dtype=float)
    if len(a)<3 or float(np.std(a))<1e-12 or float(np.std(b))<1e-12:return None
    return float(np.corrcoef(a,b)[0,1])

def build_outputs(input_path:Path, output_dir:Path):
    output_dir.mkdir(parents=True,exist_ok=True)
    rows,sts,cli,nonfuel,margins,dens=read_source(input_path)
    n=len(rows)
    if n == 0:
        raise ValueError('В основной таблице нет строк с наблюдениями')
    if len(set(r[0] for r in rows)) != n:
        raise ValueError('Обнаружены повторяющиеся ID клиентов')
    ids=[r[0] for r in rows]; station=[r[1] for r in rows];fuel=[norm_fuel(r[2]) for r in rows]
    v=np.array([r[3:12] for r in rows],dtype=np.float64)
    own=np.array([r[12:21] for r in rows],dtype=np.float64)
    comp=np.array([r[21:30] for r in rows],dtype=np.float64)
    gap=own-comp
    if not (np.isfinite(v).all() and np.isfinite(gap).all() and (v>=0).all()):raise ValueError('Invalid values or negative volume')
    group_ids=[f'{st}|{fl}' for st,fl in zip(station,fuel)]
    groups=collections.defaultdict(list)
    for i,g in enumerate(group_ids):groups[g].append(i)
    group_size=np.array([len(groups[g]) for g in group_ids])
    # Внутригрупповая нормировка: убираем общую помесячную динамику спроса на той же АЗС / топливе.
    logv=np.log1p(v); relative=np.empty_like(logv)
    for g,ixs in groups.items():
        ix=np.array(ixs,dtype=int)
        # Leave-one-out median: клиент не участвует в собственном бенчмарке.
        if len(ix)==1:
            reference=logv[ix,:].copy()
        else:
            values=logv[ix,:]
            sorted_values=np.sort(values,axis=0)
            ranks=np.argsort(np.argsort(values,axis=0),axis=0)
            others=len(ix)-1
            left=(others-1)//2;right=others//2
            left_idx=left+(left>=ranks)
            right_idx=right+(right>=ranks)
            reference=(np.take_along_axis(sorted_values,left_idx,axis=0)
                       +np.take_along_axis(sorted_values,right_idx,axis=0))/2
        relative[ix,:]=logv[ix,:]-reference
    # Индивидуальный индекс реакции, не причинная эластичность. Shrinkage защищает от 9 точек.
    x=gap-gap.mean(axis=1,keepdims=True)
    y=relative-relative.mean(axis=1,keepdims=True)
    ss=(x*x).sum(axis=1)
    slope=np.divide((x*y).sum(axis=1),ss+SHRINKAGE_PENALTY,out=np.zeros(n),where=(ss+SHRINKAGE_PENALTY)>0)
    active_months=(v>0).sum(axis=1)
    span=np.ptp(gap,axis=1)
    total=v.sum(axis=1)
    eligible=(group_size>=MIN_PEERS)&(active_months>=MIN_ACTIVE_MONTHS)&(total>=MIN_VOLUME_L)&(span>=MIN_SPAN_RUB)
    seg=np.array([segment_one(float(s),bool(e)) for s,e in zip(slope,eligible)],dtype=object)
    push=np.array([str(cli.get(i,{}).get(5))=='1' and str(cli.get(i,{}).get(7))=='1' for i in ids])
    region=[str(sts.get(st,{}).get(5,'')) for st in station]
    margin_per_liter=np.array([((margins[(fl,re)][0]-margins[(fl,re)][1])*dens[fl]/1000) if (fl,re) in margins and fl in dens else np.nan for fl,re in zip(fuel,region)])
    # Считаем финальную проверку на независимых месяцах для направленности индекса, а не причинный эффект.
    def slopes_win(a,b):
        xx=gap[:,a:b]-gap[:,a:b].mean(axis=1,keepdims=True)
        yy=relative[:,a:b]-relative[:,a:b].mean(axis=1,keepdims=True)
        return (xx*yy).sum(axis=1)/((xx*xx).sum(axis=1)+SHRINKAGE_PENALTY)
    s_first=slopes_win(0,6);s_last=slopes_win(6,9)
    split_eligible=eligible&(np.ptp(gap[:,:6],axis=1)>=0.5)&(np.ptp(gap[:,6:],axis=1)>=0.25)
    sign_agreement=float((np.sign(s_first[split_eligible])==np.sign(s_last[split_eligible])).mean()) if split_eligible.sum() else None
    # Group-level trends to indicate confounding and customer mix; no attribution.
    months=[]
    for j,m in enumerate(MONTHS):
        months.append({'month':m,'mean_liters_per_customer':round(float(v[:,j].mean()),3),'total_liters':round(float(v[:,j].sum()),2),'price_gap_rub_per_liter':round(float(gap[:,j].mean()),3),'active_share':round(float((v[:,j]>0).mean()),4), 'mean_own_price':round(float(own[:,j].mean()),3),'mean_comp_price':round(float(comp[:,j].mean()),3)})
    segments=[]
    for label in ['Высокий сигнал','Умеренный сигнал','Не выявлен','Недостаточно данных']:
        m=seg==label
        value=margin_per_liter[m]
        segments.append({'segment':label,'customers':int(m.sum()),'share_pct':round(float(m.mean()*100),2), 'total_liters_9m':round(float(total[m].sum()),2),'avg_liters_per_customer_9m':round(float(total[m].mean()),2) if m.sum() else 0,'push_opt_in_customers':int((m&push).sum()),'push_coverage_pct':round(float(push[m].mean()*100),2) if m.sum() else 0,'mean_response_index':round(float(slope[m].mean()),4) if m.sum() else 0,'mean_margin_rub_per_liter':round(float(np.nanmean(value)),2) if np.isfinite(value).any() else None})
    overlap=int((push&np.isin(seg,['Высокий сигнал','Умеренный сигнал'])).sum())
    metrics={
      'product':'МАРЖРУТ','version':'1.0.0','status':'retrospective analytical prototype, not a deployed pricing engine',
      'source_file':input_path.name,'months':MONTHS,'counts':{'customers':n,'unique_customer_ids':len(set(ids)),'total_liters_9m':round(float(v.sum()),2),'station_fuel_pairs':len(groups),'stations_in_data':len(set(station)),'stations_in_dictionary':len(sts),'client_metadata_rows':len(cli),'customers_without_metadata':int(sum(1 for i in ids if i not in cli)),'nonfuel_rows':len(nonfuel),'customers_without_nonfuel_record':int(sum(1 for i in ids if i not in nonfuel)),'push_with_app_consent':int(push.sum()),'eligible_price_signal':int(eligible.sum()),'offer_target_with_push':overlap,'margin_covered_customers':int(np.isfinite(margin_per_liter).sum())},
      'rules':{'minimum_station_fuel_peer_count':MIN_PEERS,'min_active_months':MIN_ACTIVE_MONTHS,'min_liters_9m':MIN_VOLUME_L,'min_price_gap_range_rub_per_liter':MIN_SPAN_RUB,'shrinkage_penalty_rub_squared':SHRINKAGE_PENALTY,'high_threshold':-0.08,'moderate_threshold':-0.025},
      'sanity':{'price_gap_first_month':months[0]['price_gap_rub_per_liter'],'price_gap_last_month':months[-1]['price_gap_rub_per_liter'],'mean_volume_first_month':months[0]['mean_liters_per_customer'],'mean_volume_last_month':months[-1]['mean_liters_per_customer'],'pearson_aggregate_monthly':stable_corr([a['price_gap_rub_per_liter'] for a in months],[a['mean_liters_per_customer'] for a in months]),'share_active_months_at_least_six':float((active_months>=6).mean()),'split_eligible':int(split_eligible.sum()),'split_sign_agreement':sign_agreement,'matched_margin_share':float(np.isfinite(margin_per_liter).mean())},
      'financial_inputs':{'margin_rub_per_ton_from_source':True,'opex_rub_per_ton_from_source':True,'density_kg_per_liter_from_source':True,'unit_margin_formula':'(margin_rub_per_ton - opex_rub_per_ton) * density_kg_per_liter / 1000','warning':'Assumes margin and OPEX are non-overlapping; verify accounting definition.'},
      'caveats':['Индекс относительный (сравнение с потреблением клиентов той же АЗС/топлива/месяца), не причинная эластичность.','Нельзя доказать, что ценовое изменение вызвало изменение объема: остаются общие и индивидуальные неучтенные факторы.','Одна цена для группы АЗС×топливо×месяц; клиентские строки не независимые ценовые эксперименты.','Нет реальных результатов кампаний и контрольной группы; эффект и uplift только сценарные.','Указанные пороги — экспертные, не оптимизированные по независимой разметке.','Актуальность согласий и клиента должна перепроверяться на момент коммуникации.']}
    with (output_dir/'metrics.json').open('w',encoding='utf-8') as f:json.dump(metrics,f,ensure_ascii=False,indent=2)
    def dump_csv(name,data):
        with (output_dir/name).open('w',encoding='utf-8',newline='') as f:
            w=csv.DictWriter(f,fieldnames=list(data[0].keys()),delimiter=';');w.writeheader();w.writerows(data)
    dump_csv('monthly.csv',months);dump_csv('segments.csv',segments)
    # Пилотные сценарии: агрегированные значения по сегментам, без клиентских идентификаторов.
    pilot=[]
    for label in ['Высокий сигнал','Умеренный сигнал']:
        m=(seg==label)&push&np.isfinite(margin_per_liter)
        pilot.append({'segment':label,'eligible_push_margin_customers':int(m.sum()),'mean_liters_per_month':round(float((total[m]/9).mean()),2) if m.sum() else 0,'mean_margin_rub_l':round(float(np.mean(margin_per_liter[m])),2) if m.sum() else 0})
    dump_csv('pilot_groups.csv',pilot)
    print(json.dumps({'counts':metrics['counts'],'segments':segments,'pilot':pilot,'sanity':metrics['sanity']},ensure_ascii=False,indent=2))
    return metrics,segments,months,pilot

if __name__=='__main__':
    ap=argparse.ArgumentParser(description='Offline XLSB analysis for Маржрут')
    ap.add_argument('--input',type=Path,required=True,help='Original конкурсный .xlsb')
    ap.add_argument('--output',type=Path,default=Path(__file__).resolve().parents[1]/'outputs')
    args=ap.parse_args()
    build_outputs(args.input,args.output)