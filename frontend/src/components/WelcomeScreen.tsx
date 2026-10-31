import React, { useState, useEffect } from 'react'
import { Button, Modal, Typography, Steps, Tag } from 'antd'
import {
  SearchOutlined,
  FileTextOutlined,
  TableOutlined,
  CalculatorOutlined,
  SafetyCertificateOutlined,
  CloseOutlined,
  ArrowRightOutlined,
} from '@ant-design/icons'
import { FinanceStats } from '../api/client'

const { Title, Text, Paragraph } = Typography

interface WelcomeScreenProps {
  hasDocuments?: boolean
  stats?: FinanceStats | null
  onQuickQuestion?: (question: string) => void
}

interface ExampleQuestion {
  category: string
  question: string
  icon: React.ReactNode
}

const exampleQuestions: ExampleQuestion[] = [
  {
    category: '单表查数',
    question: '美的 2025 年智能家居业务的营业收入是多少？',
    icon: <TableOutlined />,
  },
  {
    category: '跨表计算',
    question: '美的 2025 年智能家居收入占公司营业收入多少？毛利率比商业及工业解决方案高多少个百分点？',
    icon: <CalculatorOutlined />,
  },
  {
    category: '跨文档核对',
    question: '比亚迪 2024 年报和 2025 年报里披露的 2024 年营业收入是否一致？',
    icon: <FileTextOutlined />,
  },
  {
    category: '表格定位',
    question: '贵州茅台 2025 年利润表中，营业收入在第几行、对应哪个页码？',
    icon: <SearchOutlined />,
  },
  {
    category: '拒答边界',
    question: '2026 年第一季度的营业收入数据本地有吗？',
    icon: <SafetyCertificateOutlined />,
  },
]

const CoverageChip: React.FC<{ label: string; value: number | string | null | undefined }> = ({
  label,
  value,
}) => (
  <div
    style={{
      minWidth: 96,
      padding: '10px 12px',
      borderRadius: 'var(--radius-md)',
      border: '1px solid var(--color-border)',
      background: 'var(--color-bg-white)',
    }}
  >
    <div
      style={{
        fontSize: 18,
        fontWeight: 700,
        color: 'var(--color-text)',
        fontVariantNumeric: 'tabular-nums',
      }}
    >
      {value ?? '—'}
    </div>
    <div style={{ fontSize: 11, color: 'var(--color-text-tertiary)', marginTop: 2 }}>{label}</div>
  </div>
)

const WelcomeScreen: React.FC<WelcomeScreenProps> = ({
  hasDocuments = false,
  stats = null,
  onQuickQuestion,
}) => {
  const [onboardingOpen, setOnboardingOpen] = useState(false)

  useEffect(() => {
    const dismissed = localStorage.getItem('km_onboarding_dismissed')
    if (!dismissed && !hasDocuments) {
      setOnboardingOpen(true)
    }
  }, [hasDocuments])

  const dismissOnboarding = () => {
    setOnboardingOpen(false)
    localStorage.setItem('km_onboarding_dismissed', 'true')
  }

  const BrainIcon = () => (
    <svg width="52" height="52" viewBox="0 0 56 56" fill="none" style={{ opacity: 0.9 }}>
      <path
        d="M28 6C18 6 10 14 10 24c0 5 2 9 5 12-2 3-3 7-2 10 2 5 7 8 12 6 2 0 4-1 5-2 3 1 6 2 9 1 4-1 7-4 8-8 1-3 0-6-2-9 3-3 5-7 5-12 0-10-8-18-18-18z"
        stroke="currentColor"
        strokeWidth="1.8"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
      <path
        d="M18 22c1-2 3-3 5-3M22 16c2-1 4-1 6 0M33 16c2-1 4-1 6 0M38 22c1-2 3-3 5-3M18 28c2 2 4 2 6 1M33 28c2 2 4 2 6 1"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinecap="round"
      />
      <path d="M28 20v12M26 26h4" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
    </svg>
  )

  const coverage = stats
    ? [
        { label: '文档', value: stats.documents },
        { label: '章节', value: stats.sections },
        { label: '表格', value: stats.tables },
        { label: '结构块', value: stats.blocks },
      ]
    : []

  const periodText = stats?.periods?.length ? stats.periods.join(' / ') : '报告期待加载'
  const companyText = stats?.companies ? `${stats.companies} 家公司` : '语料待加载'

  if (hasDocuments) {
    return (
      <>
        <div
          className="animate-fade-in-up"
          style={{ maxWidth: 860, margin: '0 auto', padding: '32px 8px 24px' }}
        >
          <div style={{ display: 'flex', alignItems: 'flex-start', gap: 16, marginBottom: 24 }}>
            <div style={{ color: 'var(--color-primary)', marginTop: 2 }}>
              <BrainIcon />
            </div>
            <div style={{ minWidth: 0 }}>
              <Title level={3} style={{ margin: '0 0 6px', color: 'var(--color-text)' }}>
                金融查证台
              </Title>
              <Text type="secondary" style={{ fontSize: 14 }}>
                {companyText} · {periodText}
                {stats ? ` · ${stats.documents} 份文档` : ''}
              </Text>
              <div style={{ marginTop: 8 }}>
                <Tag color="cyan" style={{ margin: 0 }}>简单查数走结构检索</Tag>
                <Tag color="blue" style={{ marginLeft: 6 }}>复杂查证走多步</Tag>
                <Tag color="gold" style={{ marginLeft: 6 }}>表格/计算可定位</Tag>
              </div>
              <div style={{ marginTop: 8, fontSize: 11, color: 'var(--color-text-tertiary)', lineHeight: 1.7 }}>
                系统会自动选择路径：单点读数一次结构检索；跨表计算、跨文档核对和拒答边界交给多步查证。
              </div>
            </div>
          </div>

          <div style={{ marginBottom: 26 }}>
            <div
              style={{
                fontSize: 11,
                color: 'var(--color-text-tertiary)',
                letterSpacing: 1,
                marginBottom: 8,
              }}
            >
              查证范围
            </div>
            <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap' }}>
              {coverage.map((item) => (
                <CoverageChip key={item.label} label={item.label} value={item.value} />
              ))}
              {!stats && <CoverageChip label="语料" value="待加载" />}
            </div>
            {stats?.index_version && (
              <div style={{ fontSize: 10, color: 'var(--color-text-tertiary)', marginTop: 6 }}>
                结构索引 {stats.index_version}
                {stats.index_fingerprint ? ` · ${stats.index_fingerprint.slice(0, 10)}` : ''}
              </div>
            )}
          </div>

          <div style={{ marginBottom: 8 }}>
            <div
              style={{
                fontSize: 11,
                color: 'var(--color-text-tertiary)',
                letterSpacing: 1,
                marginBottom: 10,
              }}
            >
              试试这些查证
            </div>
            <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
              {exampleQuestions.map((item) => (
                <div
                  key={item.category}
                  role="button"
                  tabIndex={0}
                  onClick={() => onQuickQuestion?.(item.question)}
                  onKeyDown={(event) => {
                    if (event.key === 'Enter' || event.key === ' ') {
                      event.preventDefault()
                      onQuickQuestion?.(item.question)
                    }
                  }}
                  style={{
                    display: 'flex',
                    alignItems: 'center',
                    gap: 12,
                    padding: '10px 12px',
                    borderRadius: 'var(--radius-md)',
                    border: '1px solid var(--color-border)',
                    background: 'var(--color-bg-white)',
                    cursor: 'pointer',
                    transition: 'all var(--transition-fast)',
                  }}
                  onMouseEnter={(e) => {
                    e.currentTarget.style.borderColor = 'var(--color-primary)'
                    e.currentTarget.style.background = 'var(--color-primary-light)'
                  }}
                  onMouseLeave={(e) => {
                    e.currentTarget.style.borderColor = 'var(--color-border)'
                    e.currentTarget.style.background = 'var(--color-bg-white)'
                  }}
                >
                  <span style={{ color: 'var(--color-primary)', fontSize: 15 }}>{item.icon}</span>
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <div style={{ fontSize: 11, color: 'var(--color-text-tertiary)', marginBottom: 2 }}>
                      {item.category}
                    </div>
                    <div style={{ fontSize: 13, color: 'var(--color-text)', lineHeight: 1.5 }}>
                      {item.question}
                    </div>
                  </div>
                  <ArrowRightOutlined style={{ color: 'var(--color-text-tertiary)', fontSize: 11 }} />
                </div>
              ))}
            </div>
          </div>
        </div>

        <OnboardingModal open={onboardingOpen} onClose={dismissOnboarding} icon={<BrainIcon />} hasDocuments />
      </>
    )
  }

  return (
    <>
      <div
        className="animate-fade-in-up"
        style={{ maxWidth: 680, margin: '0 auto', padding: '56px 16px 40px' }}
      >
        <div style={{ textAlign: 'center', marginBottom: 28 }}>
          <div style={{ color: 'var(--color-primary)', marginBottom: 14 }}>
            <BrainIcon />
          </div>
          <Title level={3} style={{ margin: '0 0 8px', color: 'var(--color-text)' }}>
            金融查证台
          </Title>
          <Paragraph type="secondary" style={{ fontSize: 14, margin: 0 }}>
            选材料 → 提问 → 自动选路 → 看结论 → 点引用验表格与计算
          </Paragraph>
        </div>

        <div
          style={{
            padding: 20,
            borderRadius: 'var(--radius-lg)',
            border: '1px solid var(--color-border)',
            background: 'var(--color-bg-white)',
            marginBottom: 16,
          }}
        >
          <div style={{ fontSize: 13, color: 'var(--color-text-secondary)', lineHeight: 1.9 }}>
            本地还没有可查询的金融语料。导入 7 份年报/募集说明书后，
            首页会显示文档、章节、表格与结构块的真实数量。
          </div>
        </div>

        <div
          style={{
            padding: '10px 12px',
            borderRadius: 'var(--radius-md)',
            background: 'var(--color-bg-white)',
            border: '1px solid var(--color-border)',
            fontSize: 12,
            fontFamily: 'Consolas, Monaco, monospace',
            color: 'var(--color-text-secondary)',
          }}
        >
          cd backend && python -m scripts.ingest_finance_corpus
        </div>
      </div>

      <OnboardingModal open={onboardingOpen} onClose={dismissOnboarding} icon={<BrainIcon />} />
    </>
  )
}

const OnboardingModal: React.FC<{
  open: boolean
  onClose: () => void
  icon: React.ReactNode
  hasDocuments?: boolean
}> = ({ open, onClose, icon, hasDocuments = false }) => (
  <Modal
    title={null}
    open={open}
    onCancel={onClose}
    footer={null}
    width={480}
    centered
    closeIcon={<CloseOutlined style={{ fontSize: 14 }} />}
    styles={{ body: { padding: '30px 24px 24px' } }}
  >
    <div style={{ textAlign: 'center', marginBottom: 22 }}>
      <div style={{ color: 'var(--color-primary)', marginBottom: 10 }}>{icon}</div>
      <Title level={4} style={{ margin: '0 0 4px' }}>
        三步完成一次查证
      </Title>
      <Text type="secondary" style={{ fontSize: 13 }}>
        不是“聊天”，而是可回查的证据链
      </Text>
    </div>

    <Steps
      direction="vertical"
      size="small"
      current={-1}
      items={[
        {
          title: '确认材料范围',
          description: hasDocuments
            ? '首页会显示公司、报告期、章节和表格数量；先确认要查的文档在范围内。'
            : '导入金融文档，或在本地运行语料导入脚本。',
          icon: <FileTextOutlined />,
        },
        {
          title: '提出可查证的问题',
          description: '尽量带公司、年份、指标和口径。示例问题覆盖单表、跨表、跨文档、计算和拒答。',
          icon: <SearchOutlined />,
        },
        {
          title: '点引用核对证据',
          description: '右侧检查器会定位到章节、表格行列、单位和计算记录；没有结构化证据时会明确显示。',
          icon: <CalculatorOutlined />,
        },
      ]}
      style={{ marginBottom: 22 }}
    />

    <Button type="primary" block onClick={onClose} className="btn-hover">
      开始查证
    </Button>
  </Modal>
)

export default WelcomeScreen
