import React, { useState, useEffect } from 'react'
import { Button, Modal, Typography, Steps } from 'antd'
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

  const BookIcon = ({ size = 52 }: { size?: number }) => (
    <svg
      width={size}
      height={size * 1.1}
      viewBox="0 0 18 20"
      fill="none"
      aria-hidden="true"
    >
      <path
        d="M2 1.6h9l5 5v11.2a1 1 0 0 1-1 1H2a1 1 0 0 1-1-1V2.6a1 1 0 0 1 1-1z"
        stroke="currentColor"
        strokeWidth="1.1"
        strokeLinejoin="round"
      />
      <path d="M10.8 1.6V6.8h5" stroke="currentColor" strokeWidth="1.1" strokeLinejoin="round" />
      <path
        d="M4.4 10.6h6.2M4.4 13.4h4.2"
        stroke="currentColor"
        strokeWidth="1"
        strokeLinecap="round"
        opacity="0.65"
      />
    </svg>
  )

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
              <BookIcon />
            </div>
            <div style={{ minWidth: 0 }}>
              <Title level={3} style={{ margin: '0 0 6px', color: 'var(--color-text)' }}>
                金融查证台
              </Title>
              <Text type="secondary" style={{ fontSize: 14 }}>
                {companyText} · {periodText}
                {stats ? ` · ${stats.documents} 份文档` : ''}
              </Text>
              <div style={{ marginTop: 10, display: 'flex', flexWrap: 'wrap', gap: 6 }}>
                {[
                  { text: '单点读数 · 结构检索', tone: 'primary' },
                  { text: '跨表计算 · 多步查证', tone: 'primary' },
                  { text: '表格 / 计算可定位', tone: 'muted' },
                ].map((item) => (
                  <span
                    key={item.text}
                    style={{
                      padding: '2px 8px',
                      borderRadius: 'var(--radius-sm)',
                      border: '1px solid var(--color-border)',
                      background:
                        item.tone === 'primary' ? 'var(--color-primary-light)' : 'transparent',
                      color:
                        item.tone === 'primary'
                          ? 'var(--color-primary)'
                          : 'var(--color-text-secondary)',
                      fontSize: 11,
                      letterSpacing: 0.2,
                    }}
                  >
                    {item.text}
                  </span>
                ))}
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
              可定位范围
            </div>
            {/* 只报“范围”：文档/公司/期间决定能不能查。
                章节数、表格数属实现指标，压成一行脚注；结构块数是索引内部细节，不展示。 */}
            <div style={{ fontSize: 12, color: 'var(--color-text-secondary)', lineHeight: 1.7 }}>
              {stats
                ? `${stats.tables.toLocaleString()} 张表 · ${stats.sections.toLocaleString()} 个章节，每个数字都能回查页码与行列`
                : '语料待加载'}
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
                    padding: '8px 12px',
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

        <OnboardingModal open={onboardingOpen} onClose={dismissOnboarding} icon={<BookIcon />} hasDocuments />
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
            <BookIcon />
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

      <OnboardingModal open={onboardingOpen} onClose={dismissOnboarding} icon={<BookIcon />} />
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
