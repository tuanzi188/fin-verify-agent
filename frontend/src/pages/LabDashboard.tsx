import React, { useEffect, useMemo, useState } from 'react'
import {
  Alert,
  Button,
  Card,
  Col,
  Descriptions,
  Empty,
  Row,
  Space,
  Spin,
  Statistic,
  Table,
  Tag,
  Typography,
} from 'antd'
import {
  ExperimentOutlined,
  SafetyCertificateOutlined,
  ThunderboltOutlined,
} from '@ant-design/icons'
import {
  getLabRuns,
  LabArmSummary,
  LabDimensionSummary,
  LabRunSummary,
} from '../api/client'

const { Title, Text, Paragraph } = Typography

const pct = (value?: number | null): string =>
  value === null || value === undefined ? '—' : `${(value * 100).toFixed(1)}%`

const num = (value?: number | null, digits = 0): string =>
  value === null || value === undefined ? '—' : value.toFixed(digits)

const asRecord = (value: unknown): Record<string, unknown> =>
  value && typeof value === 'object' ? (value as Record<string, unknown>) : {}

const armLabel = (arm: LabArmSummary): string => {
  if (arm.label) return arm.label
  if (arm.name === 'baseline') return 'Agent baseline'
  if (arm.name === 'improved') return 'Agent improved'
  if (arm.name === 'rag') return 'RAG 对照'
  if (arm.name === 'fixed_window_rag') return '固定窗 RAG'
  if (arm.name === 'structured_rag') return '结构感知 RAG'
  return arm.name
}

const HardGateTags: React.FC<{ arm: LabArmSummary }> = ({ arm }) => {
  const hardFailTasks = arm.hard_gates?.hard_fail_tasks
  const gates = Object.entries(arm.hard_gates || {}).filter(
    ([key, value]) =>
      key !== 'overall_pass'
      && key !== 'hard_fail_tasks'
      && key !== 'hard_fail_reasons'
      && value
      && typeof value === 'object'
      && !Array.isArray(value),
  )
  if (gates.length === 0 && typeof hardFailTasks !== 'number') {
    return <Text type="secondary" style={{ fontSize: 11 }}>报告里没有硬门禁字段</Text>
  }
  return (
    <Space wrap size={[4, 4]}>
      {typeof hardFailTasks === 'number' && (
        <Tag color={hardFailTasks === 0 ? 'green' : 'red'} style={{ margin: 0, fontSize: 10 }}>
          硬门禁失败 {hardFailTasks} 题
        </Tag>
      )}
      {gates.map(([key, raw]) => {
        const item = raw as Record<string, unknown>
        const blocking = Boolean(item.blocking)
        const passed = item.pass
        return (
          <Tag
            key={key}
            color={passed === true ? 'green' : passed === false ? (blocking ? 'red' : 'orange') : 'default'}
            style={{ margin: 0, fontSize: 10 }}
          >
            {key}: {String(item.value ?? '—')} / {String(item.limit ?? '—')}
            {!blocking ? ' (advisory)' : ''}
          </Tag>
        )
      })}
    </Space>
  )
}

const ArmCard: React.FC<{ arm: LabArmSummary }> = ({ arm }) => (
  <Card size="small" style={{ height: '100%' }}>
    <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 8 }}>
      <Text strong style={{ fontSize: 14 }}>{armLabel(arm)}</Text>
      <Tag color={arm.name === 'agent' || arm.name === 'improved' ? 'blue' : 'default'} style={{ margin: 0 }}>
        {arm.tasks} 题 · {arm.runs} 次
      </Tag>
    </div>
    <Row gutter={[8, 8]} style={{ marginTop: 10 }}>
      <Col span={12}>
        <Statistic title="pass@1" value={pct(arm.pass_at_1)} valueStyle={{ fontSize: 20 }} />
      </Col>
      <Col span={12}>
        <Statistic title="pass^k" value={pct(arm.pass_pow_k)} valueStyle={{ fontSize: 20 }} />
      </Col>
      <Col span={12}>
        <Statistic
          title="token / 次"
          value={arm.tokens_per_run ? Math.round(arm.tokens_per_run) : '—'}
          valueStyle={{ fontSize: 16 }}
        />
      </Col>
      <Col span={12}>
        <Statistic
          title={arm.latency_mean_ms !== undefined && arm.latency_mean_ms !== null ? '平均延迟' : '中位延迟'}
          value={
            arm.latency_mean_ms !== undefined && arm.latency_mean_ms !== null
              ? `${Math.round(arm.latency_mean_ms)} ms`
              : arm.median_latency_ms
                ? `${Math.round(arm.median_latency_ms)} ms`
                : '—'
          }
          valueStyle={{ fontSize: 16 }}
        />
      </Col>
      <Col span={12}>
        <Statistic title="平均步数" value={num(arm.avg_steps, 2)} valueStyle={{ fontSize: 16 }} />
      </Col>
      <Col span={12}>
        <Statistic
          title="模型 / 数据"
          value={String(
            (arm.manifest || {}).model
            || (arm.name === 'rag' && (arm.manifest || {}).rag_retrieval_mode
              ? `RAG: ${(arm.manifest || {}).rag_retrieval_mode}`
              : arm.name === 'agent'
                ? 'Agent'
                : '—'),
          )}
          valueStyle={{ fontSize: 12 }}
        />
      </Col>
    </Row>
    <div style={{ marginTop: 10 }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 5, marginBottom: 4 }}>
        <SafetyCertificateOutlined style={{ color: 'var(--color-primary)', fontSize: 12 }} />
        <Text strong style={{ fontSize: 11 }}>硬门禁</Text>
      </div>
      <HardGateTags arm={arm} />
    </div>
  </Card>
)

const LabDashboard: React.FC = () => {
  const [data, setData] = useState<LabRunSummary | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  // 固定窗 RAG 只是离线辅助基线：默认收起，避免抢主比较的注意力。
  const [baselineOpen, setBaselineOpen] = useState(false)

  useEffect(() => {
    getLabRuns()
      .then(setData)
      .catch((err) => setError(err?.response?.data?.detail || err?.message || '加载失败'))
      .finally(() => setLoading(false))
  }, [])

  const dimensionColumns = useMemo(
    () => [
      { title: '维度', dataIndex: 'name', key: 'name', width: 140 },
      {
        title: `${data?.left_label || '对照臂'} pass@1`,
        dataIndex: 'baseline_pass_at_1',
        key: 'baseline',
        render: (value: number) => pct(value),
      },
      {
        title: `${data?.right_label || 'Agent'} pass@1`,
        dataIndex: 'improved_pass_at_1',
        key: 'improved',
        render: (value: number) => pct(value),
      },
      {
        title: 'delta',
        dataIndex: 'delta',
        key: 'delta',
        render: (value: number, row: LabDimensionSummary) => {
          if (value === null || value === undefined) return '—'
          const color = value > 0 ? 'green' : value < 0 ? 'red' : 'default'
          return (
            <Space size={4}>
              <Tag color={color} style={{ margin: 0 }}>
                {value > 0 ? '+' : ''}{(value * 100).toFixed(1)}pp
              </Tag>
              {row.delta_exceeds_ci_width && (
                <Tag color="blue" style={{ margin: 0, fontSize: 10 }}>超过 CI 宽度</Tag>
              )}
            </Space>
          )
        },
      },
      { title: '题数', dataIndex: 'tasks', key: 'tasks', width: 70 },
    ],
    [data?.left_label, data?.right_label],
  )

  if (loading) {
    return (
      <div style={{ padding: 48, textAlign: 'center' }}>
        <Spin />
      </div>
    )
  }

  if (error) {
    return (
      <div style={{ padding: 24 }}>
        <Alert type="error" showIcon message="实验报告加载失败" description={error} />
      </div>
    )
  }

  if (!data?.available) {
    return (
      <div style={{ padding: 24, maxWidth: 920, margin: '0 auto' }}>
        <Title level={3} style={{ marginBottom: 4 }}>
          <ExperimentOutlined style={{ marginRight: 8 }} />
          实验台：主比较尚未运行
        </Title>
        <Paragraph type="secondary">
          产品主比较固定为 <b>Agent vs 结构感知 RAG</b>。固定窗 RAG 只作为可选辅助基线，
          不进入产品主路径。这里只展示已落盘的评测产物，不在 Web 请求里跑模型。
        </Paragraph>
        <Empty
          image={Empty.PRESENTED_IMAGE_SIMPLE}
          description={data?.message || '没有找到 Agent vs 结构感知 RAG 的 compare_arms'}
        >
          <div
            style={{
              textAlign: 'left',
              background: 'var(--color-bg)',
              border: '1px solid var(--color-border)',
              borderRadius: 8,
              padding: 12,
              fontSize: 12,
              fontFamily: 'Consolas, Monaco, monospace',
              lineHeight: 1.9,
              color: 'var(--color-text-secondary)',
              maxWidth: 760,
              overflowX: 'auto',
            }}
          >
            cd backend<br />
            python -m evaluation.run_rag_eval --retrieval-mode structured --tasks evaluation/finance_tasks_complex.jsonl --out ../docs/evidence/rag_structured_runs.json<br />
            python -m evaluation.score_agent_runs --runs ../docs/evidence/rag_structured_runs.json --out ../docs/evidence/rag_structured_report.json<br />
            python -m evaluation.compare_arms --agent-report ../docs/evidence/agent_improved_report3_20261027.json --agent-runs ../docs/evidence/agent_improved_runs3_20261027.json --rag-report ../docs/evidence/rag_structured_report.json --rag-runs ../docs/evidence/rag_structured_runs.json --out ../docs/evidence/compare_arms_structured.json
          </div>
        </Empty>

        {data?.optional_baseline && (
          <Card
            size="small"
            title="可选离线基线：固定窗 RAG"
            style={{ marginTop: 16 }}
            extra={
              <Button type="link" size="small" onClick={() => setBaselineOpen((prev) => !prev)}>
                {baselineOpen ? '收起' : '展开'}
              </Button>
            }
          >
            <Alert
              type="info"
              showIcon
              message="固定窗 RAG 只是离线对照"
              description="它会拍平表格行列、单位和页码，不适合作为产品路径；保留它只为回答“结构化切片是否有收益”。"
              style={{ marginBottom: 12 }}
            />
            {baselineOpen && <ArmCard arm={data.optional_baseline} />}
          </Card>
        )}
      </div>
    )
  }

  return (
    <div style={{ padding: '20px 24px', maxWidth: 1280, margin: '0 auto' }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', gap: 12, flexWrap: 'wrap' }}>
        <div>
          <Title level={3} style={{ margin: 0 }}>
            <ExperimentOutlined style={{ marginRight: 8 }} />
            {data.comparison_kind === 'agent_vs_structured_rag'
              ? '主比较：Agent vs 结构感知 RAG'
              : data.comparison_kind === 'agent_vs_agent'
                ? '实验台：Agent 双变体对照'
                : '实验台：RAG 对照'}
          </Title>
          <Text type="secondary" style={{ fontSize: 12 }}>
            {data.generated_at || '—'} · {data.tasks_compared} 题 · 指标 {data.metric || '—'}
          </Text>
          {data.comparison_kind !== 'agent_vs_structured_rag' && (
            <div style={{ marginTop: 6 }}>
              <Tag color="orange" style={{ margin: 0 }}>
                主比较未运行
              </Tag>
              <Text type="secondary" style={{ fontSize: 11, marginLeft: 6 }}>
                当前展示的是 {data.left_label} vs {data.right_label}；固定窗 RAG 不进入产品主路径。
              </Text>
            </div>
          )}
        </div>
        <Space wrap>
          {data.verdict && (
            <Tag color={data.verdict?.includes('不可分辨') ? 'orange' : 'green'} style={{ margin: 0 }}>
              {data.verdict}
            </Tag>
          )}
          <Tag style={{ margin: 0 }}>只读产物</Tag>
        </Space>
      </div>

      <Row gutter={[12, 12]} style={{ marginTop: 16 }}>
        {data.arms.map((arm) => (
          <Col xs={24} md={12} key={arm.name}>
            <ArmCard arm={arm} />
          </Col>
        ))}
      </Row>

      {data.optional_baseline && (
        <Card
          size="small"
          title="可选离线基线：固定窗 RAG"
          style={{ marginTop: 16, borderColor: 'var(--color-border)' }}
          extra={
            <Space>
              <Tag style={{ margin: 0 }}>不进入主比较</Tag>
              <Button type="link" size="small" onClick={() => setBaselineOpen((prev) => !prev)}>
                {baselineOpen ? '收起' : '展开'}
              </Button>
            </Space>
          }
        >
          <Alert
            type="info"
            showIcon
            message="固定窗 RAG 只用于离线对照"
            description="它会把表格行列、单位和页码拍平，不适合作为产品路径。保留它只为验证“结构化切片是否有收益”。"
            style={{ marginBottom: 12 }}
          />
          {baselineOpen && <ArmCard arm={data.optional_baseline} />}
        </Card>
      )}

      <Card size="small" title="分维度 pass@1" style={{ marginTop: 16 }}>
        <Table
          size="small"
          rowKey="name"
          pagination={false}
          dataSource={data.dimensions}
          columns={dimensionColumns}
          scroll={{ x: 640 }}
        />
      </Card>

      <Row gutter={[12, 12]} style={{ marginTop: 16 }}>
        <Col xs={24} md={12}>
          <Card size="small" title="逐题配对翻转">
            <Descriptions size="small" column={2}>
              {data.comparison_kind === 'agent_vs_agent' ? (
                <>
                  <Descriptions.Item label="修好">{String(data.mcnemar.fixed ?? '—')}</Descriptions.Item>
                  <Descriptions.Item label="弄坏">{String(data.mcnemar.broke ?? '—')}</Descriptions.Item>
                </>
              ) : (
                <>
                  <Descriptions.Item label="Agent 赢（RAG 错）">
                    {String(data.mcnemar.right_win ?? '—')}
                  </Descriptions.Item>
                  <Descriptions.Item label="Agent 输（RAG 对）">
                    {String(data.mcnemar.right_loss ?? '—')}
                  </Descriptions.Item>
                </>
              )}
              <Descriptions.Item label="不一致对">{String(data.mcnemar.discordant_pairs ?? '—')}</Descriptions.Item>
              <Descriptions.Item label="p 值">
                {data.mcnemar.p_value === null || data.mcnemar.p_value === undefined
                  ? '未计算'
                  : String(data.mcnemar.p_value)}
              </Descriptions.Item>
            </Descriptions>
            {data.comparison_kind === 'agent_vs_agent' && data.mcnemar.significant === false && (
              <Alert
                type="warning"
                showIcon
                style={{ marginTop: 8 }}
                message="整卷差异未达到显著"
                description="不要写成“全卷显著提升”。可以报告分维度 delta，但分维度只做描述，不做多重比较结论。"
              />
            )}
            {data.comparison_kind !== 'agent_vs_agent' && (
              <Alert
                type="info"
                showIcon
                style={{ marginTop: 8 }}
                message="翻转数不是显著性检验"
                description="compare_arms 只记录逐题翻转；要报 p 值，需要另行做整卷配对检验。"
              />
            )}
          </Card>
        </Col>
        <Col xs={24} md={12}>
          <Card size="small" title="运行清单">
            <Descriptions size="small" column={1}>
              <Descriptions.Item label="compare 产物">
                <Text code style={{ fontSize: 11 }}>{data.compare_artifact || '—'}</Text>
              </Descriptions.Item>
              {data.arms.map((arm) => {
                const manifest = arm.manifest || {}
                return (
                  <Descriptions.Item key={arm.name} label={`${armLabel(arm)} manifest`}>
                    <div style={{ fontSize: 11, lineHeight: 1.7 }}>
                      {manifest.tasks_file ? (
                        <>
                          <div>tasks: {String(manifest.tasks_file)}</div>
                          <div>paired: {String(manifest.paired ?? '—')}</div>
                          <div>rag mode: {String(manifest.rag_retrieval_mode || '未记录')}</div>
                          <div>
                            thresholds: pass {String(asRecord(manifest.thresholds).delta_pass_rate ?? '—')}
                            {' / '}
                            token {String(asRecord(manifest.thresholds).delta_tokens_ceiling ?? '—')}
                          </div>
                        </>
                      ) : (
                        <>
                          <div>model: {String(manifest.model || '—')}</div>
                          <div>corpus: {String(manifest.corpus_hash || '—').slice(0, 16)}</div>
                          <div>tasks: {String(manifest.tasks_sha256 || '—').slice(0, 16)}</div>
                          <div>
                            git: {String(asRecord(manifest.git).commit || '—').slice(0, 12)}
                            {asRecord(manifest.git).dirty ? ' (dirty)' : ''}
                          </div>
                        </>
                      )}
                    </div>
                  </Descriptions.Item>
                )
              })}
            </Descriptions>
          </Card>
        </Col>
      </Row>

      {data.comparison_kind === 'agent_vs_structured_rag' ? (
        <Alert
          type="info"
          showIcon
          icon={<ThunderboltOutlined />}
          style={{ marginTop: 16 }}
          message="固定窗 RAG 未纳入主比较"
          description={
            <div style={{ fontSize: 12, lineHeight: 1.9 }}>
              产品主路径是“简单查数走结构检索、复杂查证走 Agent”。固定窗 RAG 只保留为离线可选基线；
              如确需辅助对照，单独运行后在 Lab 的“可选辅助基线”区域展示，不改变主比较：
              <div style={{ fontFamily: 'Consolas, Monaco, monospace', marginTop: 6 }}>
                python -m evaluation.run_rag_eval --retrieval-mode fixed --tasks evaluation/finance_tasks_complex.jsonl --out ../docs/evidence/rag_fixed_runs.json
              </div>
            </div>
          }
        />
      ) : (
        <Alert
          type="warning"
          showIcon
          icon={<ThunderboltOutlined />}
          style={{ marginTop: 16 }}
          message="主比较（Agent vs 结构感知 RAG）尚未运行"
          description={
            <div style={{ fontSize: 12, lineHeight: 1.9 }}>
              当前页展示的是 Agent 内部或旧 RAG 对照，不是产品主比较。运行结构感知 RAG 与 compare_arms 后，
              本页会自动切到主比较：
              <div style={{ fontFamily: 'Consolas, Monaco, monospace', marginTop: 6 }}>
                cd backend<br />
                python -m evaluation.run_rag_eval --retrieval-mode structured --tasks evaluation/finance_tasks_complex.jsonl --out ../docs/evidence/rag_structured_runs.json<br />
                python -m evaluation.score_agent_runs --runs ../docs/evidence/rag_structured_runs.json --out ../docs/evidence/rag_structured_report.json<br />
                python -m evaluation.compare_arms --agent-report ../docs/evidence/agent_improved_report3_20261027.json --agent-runs ../docs/evidence/agent_improved_runs3_20261027.json --rag-report ../docs/evidence/rag_structured_report.json --rag-runs ../docs/evidence/rag_structured_runs.json --out ../docs/evidence/compare_arms_structured.json
              </div>
            </div>
          }
        />
      )}
    </div>
  )
}

export default LabDashboard
